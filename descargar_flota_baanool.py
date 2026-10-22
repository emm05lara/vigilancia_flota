"""Descarga por lote (etapa 6B-2) de un GPX por vehículo con coincidencia EXACTA en BAANOOL.

Flujo: consulta los dispositivos -> lee Lista_Vehiculos.xlsx -> mapeo LLAVE <-> dispositivo ->
SOLO las coincidencias exactas -> exportación + descarga + validación de cada GPX (secuencial) ->
se guarda en entrada/<carpeta de la LLAVE>/baanool_<inicio>_<fin>.gpx.

Las fechas son SIEMPRE hora local de Ciudad de México. El token se lee de BAANOOL_TOKEN.
No analiza la flotilla al terminar y nunca borra ni sobrescribe archivos.

Uso (PowerShell):

    $env:BAANOOL_TOKEN = "<token de la sesión actual>"
    py descargar_flota_baanool.py --inicio "2026-10-10 00:00:00" --fin "2026-10-10 23:59:59" --dry-run
    py descargar_flota_baanool.py --inicio "2026-10-10 00:00:00" --fin "2026-10-10 23:59:59" --limite 3
    py descargar_flota_baanool.py --inicio "2026-10-10 00:00:00" --fin "2026-10-10 23:59:59"

Opciones: --dry-run (no llama al endpoint de exportación ni escribe manifiesto), --limite N (máximo
N exactas, por orden de LLAVE), --excel RUTA, --entrada RUTA, --manifiestos RUTA.

Al terminar (sin --dry-run) actualiza el manifiesto del periodo en salida/estado_descargas/: solo
metadatos del resultado de adquisición (no participa en la decisión de pedir GPX).
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.baanool import FORMATO_FECHA, ErrorBaanool, obtener_token
from src.descarga_lote import (CARPETA_AMBIGUA, DESCARGADO, ERROR_DESCARGA, ERROR_EXPORTACION, GPX_INVALIDO,
                               LOTE_COMPLETADO, LOTE_DETENIDO_RATE_LIMIT, LOTE_DETENIDO_TOKEN, LOTE_DRY_RUN,
                               POR_DESCARGAR, SIN_COORDENADAS, SIN_DATOS_PERIODO, YA_EXISTE, ejecutar_lote,
                               preparar_mapeo)
from src.manifiesto import CARPETA_MANIFIESTOS, ErrorEscrituraManifiesto, actualizar_manifiesto
from src.parametros import PAUSA_ENTRE_DESCARGAS_SEGUNDOS

RAIZ = Path(__file__).resolve().parent


def fecha(texto: str) -> datetime:
    try:
        return datetime.strptime(texto, FORMATO_FECHA)
    except ValueError:
        raise argparse.ArgumentTypeError(f"fecha inválida {texto!r}: usa el formato 'YYYY-MM-DD HH:MM:SS'")


def entero_positivo(texto: str) -> int:
    try:
        valor = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{texto!r} no es un entero")
    if valor < 1:
        raise argparse.ArgumentTypeError("debe ser 1 o mayor")
    return valor


def imprimir_progreso(indice: int, total: int, resultado) -> None:
    print(f"[{indice}/{total}] {resultado.llave}  {resultado.estado}")


def imprimir_resumen(lote, dry_run: bool) -> None:
    m = lote.resumen_mapeo
    no_elegibles = m["posibles"] + m["solo_baanool"] + m["conflictos"]
    print("\nDESCARGA BAANOOL" + ("  (DRY-RUN: no se llamó al endpoint de exportación)" if dry_run else ""))
    print("\nPeriodo local (America/Mexico_City):")
    print(f"  inicio: {lote.inicio:%Y-%m-%d %H:%M:%S}")
    print(f"  fin:    {lote.fin:%Y-%m-%d %H:%M:%S}")
    print("\nMapeo:")
    print(f"  exactos (elegibles):   {lote.exactas_totales}")
    print(f"  seleccionados:         {lote.seleccionadas}")
    print(f"  solo local:            {m['solo_local']}")
    print(f"  no elegibles:          {no_elegibles}  (posibles {m['posibles']}, solo BAANOOL "
          f"{m['solo_baanool']}, conflictos {m['conflictos']})")
    print("\nResultado:")
    if dry_run:
        print(f"  se descargarían:       {lote.cuenta(POR_DESCARGAR)}")
    else:
        print(f"  descargados:           {lote.cuenta(DESCARGADO)}")
    print(f"  ya existentes:         {lote.cuenta(YA_EXISTE)}")
    print(f"  sin datos en el periodo: {lote.cuenta(SIN_DATOS_PERIODO)}  (no es un error)")
    print(f"  sin coordenadas:       {lote.cuenta(SIN_COORDENADAS)}")
    print(f"  errores:               {len(lote.errores)}")
    print(f"  procesados:            {lote.procesados}")
    if lote.pendientes:
        print(f"  pendientes:            {len(lote.pendientes)}")
    print(f"\nTiempo total: {lote.segundos:.1f} s")

    if lote.estado in (LOTE_DETENIDO_RATE_LIMIT, LOTE_DETENIDO_TOKEN):
        print(f"\n{lote.estado}: el lote se detuvo y no se reintentó"
              + (f" (en {lote.detenido_en})" if lote.detenido_en else ""))
        print(f"  motivo: {lote.motivo_detencion}")
        print(f"  completados antes de detenerse: {lote.cuenta(DESCARGADO)} descargados, "
              f"{lote.cuenta(YA_EXISTE)} ya existentes")
        print("  Los GPX ya descargados permanecen intactos.")

    sin_datos = [v for v in lote.vehiculos if v.estado == SIN_DATOS_PERIODO]
    if sin_datos:
        print()
        print(f"SIN DATOS EN EL PERIODO ({len(sin_datos)}): BAANOOL respondió pero no entregó posiciones GPS.")
        print("  No se infiere la causa. El estado actual es una fotografía y NO describe el periodo.")
        for v in sin_datos:
            print(f"  {v.llave}    (estado actual en BAANOOL: {v.estado_actual_baanool or 'sin dato'})")

    excepciones = [v for v in lote.vehiculos if v.estado in (
        ERROR_EXPORTACION, ERROR_DESCARGA, GPX_INVALIDO, CARPETA_AMBIGUA, SIN_COORDENADAS)]
    if excepciones:
        print(f"\nERRORES Y EXCEPCIONES ({len(excepciones)})")
        for v in excepciones:
            print(f"  {v.llave}\n    {v.estado}: {v.mensaje}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Descarga por lote de GPX desde BAANOOL (coincidencias exactas)")
    parser.add_argument("--inicio", required=True, type=fecha, help="'YYYY-MM-DD HH:MM:SS' (hora CDMX)")
    parser.add_argument("--fin", required=True, type=fecha, help="'YYYY-MM-DD HH:MM:SS' (hora CDMX)")
    parser.add_argument("--dry-run", action="store_true", help="no llama al endpoint de exportación")
    parser.add_argument("--limite", type=entero_positivo, help="máximo N coincidencias exactas (por orden de LLAVE)")
    parser.add_argument("--excel", type=Path, default=RAIZ / "Lista_Vehiculos.xlsx")
    parser.add_argument("--entrada", type=Path, default=RAIZ / "entrada")
    parser.add_argument("--manifiestos", type=Path, default=CARPETA_MANIFIESTOS,
                        help="carpeta del manifiesto de adquisición (por defecto salida/estado_descargas/)")
    args = parser.parse_args()
    if args.fin <= args.inicio:
        parser.error("--fin debe ser posterior a --inicio")

    for flujo in (sys.stdout, sys.stderr):  # los mensajes de BAANOOL pueden traer caracteres chinos
        flujo.reconfigure(errors="backslashreplace")

    try:
        token = obtener_token()
        print("Consultando dispositivos en BAANOOL...")
        mapeo, advertencias = preparar_mapeo(token, args.excel)
    except ErrorBaanool as exc:
        print(f"\nERROR [{exc.codigo}]: {exc.mensaje}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nERROR al leer {args.excel.name}: {exc}", file=sys.stderr)
        return 1
    if advertencias:
        print(f"ADVERTENCIA: {advertencias} elemento(s) de la lista de dispositivos se ignoraron.")

    print(f"Periodo local: {args.inicio:%Y-%m-%d %H:%M:%S} -> {args.fin:%Y-%m-%d %H:%M:%S}")
    print(f"Secuencial, pausa de {PAUSA_ENTRE_DESCARGAS_SEGUNDOS} s entre vehículos."
          + (" DRY-RUN." if args.dry_run else "") + "\n")

    lote = ejecutar_lote(mapeo, args.inicio, args.fin, args.entrada, token=token, dry_run=args.dry_run,
                         limite=args.limite, progreso=imprimir_progreso)
    imprimir_resumen(lote, args.dry_run)

    fallo_manifiesto = False
    if not args.dry_run:  # un dry-run nunca escribe el manifiesto real
        try:
            ruta = actualizar_manifiesto(lote, mapeo, RAIZ, args.manifiestos)
            try:
                mostrada = ruta.relative_to(RAIZ)
            except ValueError:
                mostrada = ruta
            print()
            print(f"Manifiesto de adquisición actualizado: {mostrada}")
        except ErrorEscrituraManifiesto as exc:
            fallo_manifiesto = True
            print(f"\nERROR: no se pudo actualizar el manifiesto: {exc}. Los GPX descargados no se afectan.",
                  file=sys.stderr)
    ok = lote.estado in (LOTE_COMPLETADO, LOTE_DRY_RUN) and not lote.errores and not fallo_manifiesto
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
