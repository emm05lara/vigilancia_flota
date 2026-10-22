"""Vigilancia de flota - etapa 3: geofencing CDMX + Estado de México, sobre movimiento y paradas.

Uso:
    py main.py                          # recorre entrada/ (una carpeta por LLAVE)
    py main.py --entrada OTRA_CARPETA   # otra carpeta de entrada
    py main.py --gpx datos/D28BET.gpx   # un solo GPX, con detalle
    py main.py --gpx datos/D28BET.gpx --mapa   # además genera salida/<nombre>_mapa.html
"""

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from src.analizador import analizar_flota, analizar_gpx
from src.llaves import buscar_excel_en, cargar_llaves, indexar_por_carpeta, limpiar_nombre_carpeta
from src.geocercas import asociar_con_trayectos
from src.parametros import (DISTANCIA_MINIMA_TRAYECTO_KM, PUNTOS_MINIMOS_FUERA, RADIO_PARADA_KM,
                            TOLERANCIA_DUPLICADO_METROS, TOLERANCIA_FRONTERA_METROS, UMBRAL_HUECO_MINUTOS)
from src.zonas import cargar_zonas

RAIZ = Path(__file__).resolve().parent


# ---------- presentación ----------

def fmt_fecha(f: datetime | None) -> str:
    return f.strftime("%Y-%m-%d %H:%M:%S") if f else "-"


def fmt_duracion(d: timedelta | None) -> str:
    if d is None:
        return "-"
    horas, resto = divmod(int(d.total_seconds()), 3600)
    minutos, segundos = divmod(resto, 60)
    return f"{horas}h {minutos:02d}m {segundos:02d}s"


def fmt_coord(c: tuple[float, float] | None) -> str:
    return f"{c[0]:.6f}, {c[1]:.6f}" if c else "-"


def imprimir_resumen(resumen, sangria: str = "  ") -> None:
    velocidad = resumen.velocidad_media_maxima_kmh
    filas = [
        ("Puntos", resumen.cantidad_puntos),
        ("Fecha inicial", fmt_fecha(resumen.fecha_inicial)),
        ("Fecha final", fmt_fecha(resumen.fecha_final)),
        ("Duración", fmt_duracion(resumen.duracion)),
        ("Coordenada inicial", fmt_coord(resumen.coordenada_inicial)),
        ("Coordenada final", fmt_coord(resumen.coordenada_final)),
        ("Distancia estimada observada", f"{resumen.distancia_estimada_observada_km:.2f} km"),
        ("Distancia ruido paradas", f"{resumen.distancia_ruido_paradas_km:.2f} km  (no se suma)"),
        ("Desplaz. mínimo huecos", f"{resumen.desplazamiento_minimo_huecos_km:.2f} km  (no se suma)"),
        ("Distancia bruta (diagnóstico)", f"{resumen.distancia_bruta_km:.2f} km"),
        ("Intervalo mínimo", fmt_duracion(resumen.intervalo_minimo)),
        ("Intervalo mediano", fmt_duracion(resumen.intervalo_mediano)),
        ("Intervalo máximo", fmt_duracion(resumen.intervalo_maximo)),
        ("Vel. media máx. entre puntos", f"{velocidad:.1f} km/h" if velocidad is not None else "-"),
    ]
    for etiqueta, valor in filas:
        print(f"{sangria}{etiqueta:<31} {valor}")


def imprimir_consolidacion(duplicados: int, conflictos, sangria: str = "  ") -> None:
    print(f"{sangria}{'Duplicados eliminados':<31} {duplicados}")
    print(f"{sangria}{'Conflictos de timestamp':<31} {len(conflictos)}")
    for c in conflictos:
        print(f"{sangria}  - {fmt_fecha(c.fecha)}: {fmt_coord(c.coordenada_conservada)} (conservada) vs "
              f"{fmt_coord(c.coordenada_excluida)} (excluida), {c.distancia_m:.0f} m")


def imprimir_actividad(resumen, actividad, sangria: str = "  ") -> None:
    print(f"{sangria}Umbral de intervalo largo: {UMBRAL_HUECO_MINUTOS} min | radio de parada probable: "
          f"{RADIO_PARADA_KM:.2f} km | distancia mínima de trayecto: {DISTANCIA_MINIMA_TRAYECTO_KM:.2f} km | "
          f"tolerancia de duplicado: {TOLERANCIA_DUPLICADO_METROS} m\n")
    filas = [
        ("Paradas probables", actividad.cantidad_paradas_probables),
        ("Tiempo en paradas probables", fmt_duracion(actividad.tiempo_en_paradas_probables)),
        ("Huecos con desplazamiento", actividad.cantidad_huecos_con_desplazamiento),
        ("Trayectos", len(actividad.trayectos)),
        ("Primera actividad", fmt_fecha(actividad.primera_actividad)),
        ("Última actividad", fmt_fecha(actividad.ultima_actividad)),
    ]
    for etiqueta, valor in filas:
        print(f"{sangria}{etiqueta:<30} {valor}")

    print(f"\n{sangria}Trayectos (la duración es el periodo de actividad reportado, no tiempo en movimiento):")
    for i, t in enumerate(actividad.trayectos, start=1):
        print(f"{sangria}  {i:>2}. {fmt_fecha(t.inicio)} -> {fmt_fecha(t.fin)}  {fmt_duracion(t.duracion)}  "
              f"{t.cantidad_puntos:>3} pts  {t.distancia_estimada_observada_km:>6.2f} km obs.  "
              f"{fmt_coord(t.coordenada_inicial)} -> {fmt_coord(t.coordenada_final)}")

    print(f"\n{sangria}Paradas probables (intervalo > {UMBRAL_HUECO_MINUTOS} min, extremos a <= {RADIO_PARADA_KM:.2f} km):")
    for p in resumen.paradas_probables:
        print(f"{sangria}  - {fmt_fecha(p.inicio)} -> {fmt_fecha(p.fin)}  "
              f"({fmt_duracion(p.duracion)}, {p.distancia_km:.2f} km entre extremos)")

    print(f"\n{sangria}Huecos con desplazamiento: {len(resumen.huecos_con_desplazamiento)}")
    for h in resumen.huecos_con_desplazamiento:
        print(f"{sangria}  - {fmt_fecha(h.inicio)} -> {fmt_fecha(h.fin)}  "
              f"({fmt_duracion(h.duracion)}, desplazamiento mínimo conocido {h.distancia_km:.2f} km en línea recta; "
              f"trayectoria desconocida)")


def descripcion_evento(e) -> str:
    """Una línea por evento, siempre como ventanas de incertidumbre (nunca una hora de cruce)."""
    if e.inicia_fuera:
        salida = "el periodo empieza fuera de la zona (salida no observada)"
    else:
        salida = (f"salida entre {fmt_fecha(e.ultimo_punto_dentro.punto.fecha)} y "
                  f"{fmt_fecha(e.primer_punto_fuera.punto.fecha)}")
    if e.abierta:
        regreso = "SIN REGRESO observado (termina fuera)"
    else:
        regreso = (f"regreso entre {fmt_fecha(e.ultimo_punto_fuera.punto.fecha)} y "
                   f"{fmt_fecha(e.primer_punto_dentro_posterior.punto.fecha)}")
    texto = (f"{salida}; {regreso}; entidades: {', '.join(e.entidades)}; "
             f"{e.cantidad_puntos_fuera} pts fuera; duración observable fuera {fmt_duracion(e.duracion_observable_fuera)}")
    if e.duracion_maxima_posible is not None:
        texto += f" (máx. posible {fmt_duracion(e.duracion_maxima_posible)})"
    if e.salida_con_hueco or e.regreso_con_hueco or e.huecos_durante_evento:
        texto += "; INFORMACIÓN INCOMPLETA: hueco con desplazamiento, trayectoria desconocida"
    return texto


def imprimir_geofencing(geo, actividad, sangria: str = "  ") -> None:
    print(f"{sangria}GEOFENCING")
    print(f"{sangria}Zona permitida: Ciudad de México + Estado de México "
          f"(Marco Geoestadístico INEGI, referencia operativa)")
    print(f"{sangria}Tolerancia de frontera: {TOLERANCIA_FRONTERA_METROS} m | "
          f"puntos fuera mínimos para confirmar: {PUNTOS_MINIMOS_FUERA}\n")
    print(f"{sangria}{'Puntos dentro':<26} {geo.puntos_dentro}")
    print(f"{sangria}{'Puntos tolerancia':<26} {geo.puntos_tolerancia}")
    print(f"{sangria}{'Puntos fuera':<26} {geo.puntos_fuera}\n")
    print(f"{sangria}{'Salidas confirmadas':<26} {geo.salidas_confirmadas}")
    print(f"{sangria}{'Salidas no confirmadas':<26} {geo.salidas_no_confirmadas}")
    if geo.inicia_fuera:
        print(f"{sangria}{'Inicia fuera de zona':<26} Sí (la salida no fue observada)")
    print(f"\n{sangria}Entidades fuera:")
    if geo.entidades_fuera:
        for nombre in geo.entidades_fuera:
            print(f"{sangria}  - {nombre}")
    else:
        print(f"{sangria}  (ninguna)")
    print(f"\n{sangria}Terminó fuera: {'Sí' if geo.termino_fuera else 'No'}")

    if not geo.eventos:
        print(f"\n{sangria}SIN SALIDAS CONFIRMADAS DE LA ZONA PERMITIDA")
        return
    if not geo.tuvo_salida_confirmada and not geo.inicia_fuera:
        print(f"\n{sangria}SIN SALIDAS CONFIRMADAS DE LA ZONA PERMITIDA")
    print(f"\n{sangria}Eventos:")
    for e in geo.eventos:
        etiqueta = ("INICIO FUERA DE ZONA" if e.inicia_fuera and e.confirmado
                    else "SALIDA CONFIRMADA" if e.confirmado else "salida no confirmada")
        print(f"{sangria}  [{etiqueta}] {descripcion_evento(e)}")

    asociados = asociar_con_trayectos(geo.eventos, actividad.trayectos)
    if asociados:
        print(f"\n{sangria}Trayectos con salida de zona:")
        for numero, eventos in asociados.items():
            t = actividad.trayectos[numero - 1]
            entidades = sorted({n for e in eventos for n in e.entidades})
            print(f"{sangria}  Trayecto {numero}: {t.distancia_estimada_observada_km:.2f} km observados | "
                  f"salió de zona: sí | entidades: {', '.join(entidades)}")


def cargar_zonas_si_existen(carpeta: Path):
    """Carga el GeoJSON local; sin él el análisis continúa sin geofencing (nunca descarga)."""
    try:
        return cargar_zonas(carpeta)
    except FileNotFoundError as exc:
        print(f"AVISO: geofencing desactivado. {exc}\n")
        return None


def imprimir_calidad_gps(c, sangria: str = "  ") -> None:
    """Diagnóstico solamente: no modifica ninguna métrica oficial."""
    print(f"{sangria}CALIDAD GPS (diagnóstico)  estado: {c.estado}")
    vmax = f"{c.velocidad_maxima_kmh:.0f} km/h" if c.velocidad_maxima_kmh is not None else "-"
    print(f"{sangria}{'Velocidad máxima estimada':<34} {vmax}")
    print(f"{sangria}{'Tramos con velocidad sospechosa':<34} {len(c.tramos_velocidad_sospechosa)}")
    print(f"{sangria}{'Saltos GPS probables':<34} {len(c.saltos_probables)}")
    print(f"{sangria}{'Anomalías GPS complejas':<34} {len(c.anomalias_complejas)}")
    print(f"{sangria}{'Distancia oficial':<34} {c.distancia_oficial_km:.2f} km")
    print(f"{sangria}{'Distancia depurada (experimental)':<34} {c.distancia_depurada_km:.2f} km")
    print(f"{sangria}{'Diferencia por saltos':<34} {c.distancia_atribuida_saltos_km:.2f} km "
          f"({c.diferencia_porcentual:.2f} %)")
    for s in c.saltos_probables:
        print(f"{sangria}  - salto probable en {fmt_fecha(s.punto_sospechoso.fecha)}: A-X {s.distancia_ax_km:.2f} km "
              f"({s.velocidad_ax_kmh:.0f} km/h), X-B {s.distancia_xb_km:.2f} km ({s.velocidad_xb_kmh:.0f} km/h), "
              f"A-B {s.distancia_ab_km:.2f} km, relación {s.relacion_desvio:.1f}")


def escribir_mapa(puntos, titulo: str, nombre_base: str, carpeta_salida: Path,
                  geofencing=None, zonas=None, calidad=None) -> None:
    from src.mapa import generar_mapa  # Folium solo se carga si se pide un mapa

    ruta = generar_mapa(puntos, titulo, carpeta_salida / f"{limpiar_nombre_carpeta(nombre_base)}_mapa.html",
                        geofencing, zonas, calidad)
    print(f"  Mapa: {ruta}")


def modo_gpx(ruta: Path, con_mapa: bool, carpeta_salida: Path) -> None:
    zonas = cargar_zonas_si_existen(RAIZ / "zonas")
    resultado = analizar_gpx(ruta, zonas)
    print(f"\nArchivo: {ruta}")
    if resultado.error:
        print(f"  ERROR: {resultado.error}")
        return
    if resultado.puntos_descartados:
        print(f"  Puntos descartados por datos inválidos: {resultado.puntos_descartados}")
    print("  (Hora local asumida: America/Mexico_City. La distancia es una estimación "
          "entre puntos, no kilometraje real)\n")
    imprimir_resumen(resultado.resumen)
    imprimir_consolidacion(resultado.duplicados_eliminados, resultado.conflictos_timestamp)
    print()
    imprimir_actividad(resultado.resumen, resultado.actividad)
    if resultado.geofencing:
        print()
        imprimir_geofencing(resultado.geofencing, resultado.actividad)
    if resultado.calidad_gps:
        print()
        imprimir_calidad_gps(resultado.calidad_gps)
    if con_mapa and resultado.puntos:
        print()
        escribir_mapa(resultado.puntos, ruta.name, ruta.stem, carpeta_salida, resultado.geofencing, zonas,
                      resultado.calidad_gps)


def modo_flota(carpeta_entrada: Path, archivo_excel: Path, con_mapa: bool, carpeta_salida: Path) -> None:
    llaves = cargar_llaves(archivo_excel)
    indice = indexar_por_carpeta(llaves)
    print(f"Excel: {archivo_excel.name} ({len(llaves)} LLAVES)")
    print(f"Entrada: {carpeta_entrada}")

    zonas = cargar_zonas_si_existen(RAIZ / "zonas")
    vehiculos = analizar_flota(carpeta_entrada, indice, zonas)
    if not vehiculos:
        print("\nNo hay carpetas de vehículos en la carpeta de entrada.")
        return

    print(f"\n{'':<3}{'LLAVE':<34}{'GPX':>4}{'Dist.obs.km':>13}{'Trayec.':>8}{'Salidas':>8}"
          f"{'Inicia fuera':>13}{'Fuera final':>12}  Entidades fuera")
    print("-" * 115)
    con_salidas = []
    for v in vehiculos:
        nombre = v.llave_original or f"[SIN LLAVE] {v.nombre_carpeta}"
        if v.resumen is None:
            print(f"{'':<3}{nombre:<34}{v.numero_gpx:>4}  (sin archivos .gpx)")
            continue
        r, a, g = v.resumen, v.actividad, v.geofencing
        if g is None:
            salidas = inicia = final = "n/d"
            entidades = ""
        else:
            salidas = str(g.salidas_confirmadas)
            inicia = "Sí" if g.inicia_fuera else "No"
            final = "Sí" if g.termino_fuera else "No"
            entidades = ", ".join(g.entidades_fuera)
        atencion = g is not None and (g.tuvo_salida_confirmada or g.inicia_fuera)
        if atencion:
            con_salidas.append(v)
        print(f"{'>>' if atencion else '':<3}{nombre:<34}{v.numero_gpx:>4}{r.distancia_estimada_observada_km:>13.2f}"
              f"{len(a.trayectos):>8}{salidas:>8}{inicia:>13}{final:>12}  {entidades}")
        for gpx in v.gpx:
            if gpx.error:
                print(f"{'':<3}  ! {gpx.archivo.name}: {gpx.error}")
        if con_mapa and v.puntos:
            escribir_mapa(v.puntos, nombre, v.llave_original or v.nombre_carpeta, carpeta_salida,
                          g, zonas, v.calidad_gps)

    if con_salidas:
        print("\nVEHÍCULOS CON SALIDAS DE LA ZONA PERMITIDA (>>)")
        for v in con_salidas:
            print(f"  {v.llave_original or v.nombre_carpeta}")
            for e in v.geofencing.eventos:
                if e.confirmado:
                    print(f"    - {descripcion_evento(e)}")
    elif zonas:
        print("\nSIN SALIDAS CONFIRMADAS DE LA ZONA PERMITIDA en ningún vehículo")

    con_gpx = sum(1 for v in vehiculos if v.resumen is not None)
    sin_llave = [v.nombre_carpeta for v in vehiculos if v.llave_original is None]
    conflictos = sum(len(v.conflictos_timestamp) for v in vehiculos)
    print(f"\nCarpetas: {len(vehiculos)} | con GPX: {con_gpx} | LLAVES en Excel: {len(llaves)}")
    if conflictos:
        print(f"Conflictos de timestamp en total: {conflictos} (misma fecha/hora en lugares distintos; "
              f"se conservó el primer punto)")
    if sin_llave:
        print(f"Carpetas que no corresponden a ninguna LLAVE del Excel: {len(sin_llave)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Análisis por lote de GPX de BAANOOL")
    parser.add_argument("--gpx", type=Path, help="analizar un solo archivo GPX")
    parser.add_argument("--entrada", type=Path, default=RAIZ / "entrada",
                        help="carpeta con una subcarpeta por LLAVE")
    parser.add_argument("--excel", type=Path,
                        help="Excel con la columna LLAVE (por defecto, el .xlsx de la raíz)")
    parser.add_argument("--mapa", action="store_true", help="generar mapas HTML de validación (requiere folium)")
    parser.add_argument("--salida", type=Path, default=RAIZ / "salida", help="carpeta de los mapas")
    args = parser.parse_args()

    if args.gpx:
        modo_gpx(args.gpx, args.mapa, args.salida)
        return
    if not args.entrada.is_dir():
        raise SystemExit(f"No existe la carpeta de entrada: {args.entrada}")
    modo_flota(args.entrada, args.excel or buscar_excel_en(RAIZ), args.mapa, args.salida)


if __name__ == "__main__":
    main()
