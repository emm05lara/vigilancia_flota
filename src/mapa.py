"""Mapa HTML de validación (Folium). Es opcional: solo se importa al pedir un mapa."""

from html import escape
from pathlib import Path

import folium
from shapely.geometry import mapping

from .calidad_gps import ResumenCalidadGps
from .geocercas import EventoSalida, ResumenGeofencing, asociar_con_trayectos
from .gpx_parser import Punto
from .metricas import HUECO_CON_DESPLAZAMIENTO, PARADA_PROBABLE, calcular_tramos
from .movimiento import construir_trayectos
from .zonas import Zonas

COLORES_TRAYECTOS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22"]
FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"


def _duracion(d) -> str:
    horas, resto = divmod(int(d.total_seconds()), 3600)
    return f"{horas}h {resto // 60:02d}m"


def _fecha(clasificado) -> str:
    return clasificado.punto.fecha.strftime(FORMATO_FECHA)


def _coord(clasificado) -> tuple[float, float]:
    return clasificado.punto.latitud, clasificado.punto.longitud


def _dibujar_zona_permitida(mapa, zonas: Zonas) -> None:
    capa = folium.FeatureGroup("Zona permitida (CDMX + Estado de México)").add_to(mapa)
    for entidad in zonas.entidades:
        if entidad.clave not in zonas.claves_permitidas:
            continue
        simplificada = entidad.geometria.simplify(0.0003, preserve_topology=True)  # solo para dibujar
        folium.GeoJson(
            mapping(simplificada), name=entidad.nombre,
            style_function=lambda _f: {"color": "#2e7d32", "weight": 2, "fillColor": "#66bb6a", "fillOpacity": 0.06},
            tooltip=f"{entidad.nombre} (INEGI, referencia operativa)",
        ).add_to(capa)


def _texto_evento(e: EventoSalida) -> str:
    lineas = []
    if e.inicia_fuera:
        lineas.append("<b>El periodo analizado empieza fuera de la zona</b><br>"
                      "(no se observó la salida)")
    else:
        lineas.append(f"Salida detectada entre {_fecha(e.ultimo_punto_dentro)} y {_fecha(e.primer_punto_fuera)}"
                      " (hora exacta de cruce desconocida)")
    lineas.append(f"Primer punto fuera: {_fecha(e.primer_punto_fuera)}")
    lineas.append(f"Último punto fuera: {_fecha(e.ultimo_punto_fuera)}")
    if e.abierta:
        lineas.append("<b>Sin regreso observado: el periodo termina fuera de la zona</b>")
    else:
        lineas.append(f"Regreso detectado entre {_fecha(e.ultimo_punto_fuera)} y "
                      f"{_fecha(e.primer_punto_dentro_posterior)}")
    lineas.append(f"Entidades: {', '.join(e.entidades)}")
    lineas.append(f"Puntos fuera: {e.cantidad_puntos_fuera} | en tolerancia: {e.cantidad_puntos_tolerancia}")
    lineas.append(f"Duración observable fuera: {_duracion(e.duracion_observable_fuera)} "
                  "(solo entre puntos observados fuera)")
    if e.salida_con_hueco or e.regreso_con_hueco or e.huecos_durante_evento:
        lineas.append("<b>Información incompleta:</b> hubo un hueco con desplazamiento "
                      "(trayectoria desconocida)")
    return "<br>".join(lineas)


def _dibujar_geofencing(mapa, geofencing: ResumenGeofencing) -> None:
    capa_salidas = folium.FeatureGroup("Salidas de la zona").add_to(mapa)
    capa_no_conf = folium.FeatureGroup("Salidas no confirmadas (diagnóstico)", show=False).add_to(mapa)

    for e in geofencing.eventos:
        if not e.confirmado:
            folium.CircleMarker(
                _coord(e.primer_punto_fuera), radius=5, color="#757575", fill=True,
                popup=folium.Popup("<b>Salida NO confirmada</b> (menos de los puntos fuera mínimos)<br>"
                                   + _texto_evento(e), max_width=350),
            ).add_to(capa_no_conf)
            continue

        # Excursión: del último punto dentro, por todos los puntos del periodo, hasta el regreso.
        recorrido = [_coord(c) for c in e.puntos_periodo]
        if e.ultimo_punto_dentro:
            recorrido.insert(0, _coord(e.ultimo_punto_dentro))
        if e.primer_punto_dentro_posterior:
            recorrido.append(_coord(e.primer_punto_dentro_posterior))
        folium.PolyLine(recorrido, color="#c2185b", weight=7, opacity=0.55).add_to(capa_salidas)

        if e.inicia_fuera:
            titulo = "Inicio del periodo fuera de la zona"
            icono = folium.Icon(color="orange", icon="flag")
        else:
            titulo = "Salida confirmada"
            icono = folium.Icon(color="darkred", icon="log-out")
        folium.Marker(_coord(e.primer_punto_fuera), icon=icono,
                      popup=folium.Popup(f"<b>{titulo}</b><br>" + _texto_evento(e), max_width=350),
                      tooltip=titulo).add_to(capa_salidas)

        if e.primer_punto_dentro_posterior is not None:
            folium.Marker(_coord(e.primer_punto_dentro_posterior), icon=folium.Icon(color="blue", icon="log-in"),
                          popup=f"<b>Regreso detectado</b><br>Primer punto dentro: "
                                f"{_fecha(e.primer_punto_dentro_posterior)}<br>Último punto fuera: "
                                f"{_fecha(e.ultimo_punto_fuera)}",
                          tooltip="Regreso").add_to(capa_salidas)
        else:
            folium.Marker(_coord(e.ultimo_punto_fuera), icon=folium.Icon(color="black", icon="exclamation-sign"),
                          popup=f"<b>Evento abierto: termina fuera de la zona</b><br>Último punto: "
                                f"{_fecha(e.ultimo_punto_fuera)}<br>Entidades: {', '.join(e.entidades)}",
                          tooltip="Termina fuera").add_to(capa_salidas)


def _dibujar_calidad_gps(mapa, calidad: ResumenCalidadGps) -> None:
    """Capa de diagnóstico. No modifica la trayectoria oficial ni dibuja una ruta "corregida" como real."""
    if not (calidad.saltos_probables or calidad.anomalias_complejas):
        return
    capa = folium.FeatureGroup("Calidad GPS (diagnóstico)").add_to(mapa)

    for s in calidad.saltos_probables:
        a, x, b = s.punto_anterior, s.punto_sospechoso, s.punto_siguiente
        relacion = "∞" if s.relacion_desvio == float("inf") else f"{s.relacion_desvio:.1f}"
        texto = (f"<b>SALTO GPS PROBABLE</b><br>{x.fecha.strftime(FORMATO_FECHA)}"
                 f"<br>Coordenada original: {x.latitud:.5f}, {x.longitud:.5f}"
                 f"<br>A-X: {s.distancia_ax_km:.2f} km en {s.duracion_ax_segundos:.0f} s "
                 f"({s.velocidad_ax_kmh:.0f} km/h)"
                 f"<br>X-B: {s.distancia_xb_km:.2f} km en {s.duracion_xb_segundos:.0f} s "
                 f"({s.velocidad_xb_kmh:.0f} km/h)"
                 f"<br>A-B: {s.distancia_ab_km:.2f} km ({s.velocidad_ab_kmh:.0f} km/h)"
                 f"<br>Relación de desvío: {relacion}"
                 f"<br>Distancia atribuida (experimental): {s.distancia_atribuida_km:.2f} km"
                 f"<br><i>Diagnóstico: el punto NO se elimina de las métricas oficiales.</i>")
        folium.Marker((x.latitud, x.longitud), icon=folium.Icon(color="purple", icon="warning-sign"),
                      popup=folium.Popup(texto, max_width=360), tooltip="Salto GPS probable").add_to(capa)
        folium.PolyLine([(a.latitud, a.longitud), (x.latitud, x.longitud)], color="#e65100", weight=3,
                        dash_array="4", tooltip="A → X (diagnóstico)").add_to(capa)
        folium.PolyLine([(x.latitud, x.longitud), (b.latitud, b.longitud)], color="#e65100", weight=3,
                        dash_array="4", tooltip="X → B (diagnóstico)").add_to(capa)
        folium.PolyLine([(a.latitud, a.longitud), (b.latitud, b.longitud)], color="#6a1b9a", weight=2,
                        dash_array="10", opacity=0.9,
                        tooltip="Conexión experimental A → B sin el punto sospechoso (no es la ruta real)"
                        ).add_to(capa)

    for an in calidad.anomalias_complejas:
        if not an.puntos_sospechosos:
            continue
        x = an.puntos_sospechosos[0]
        texto = (f"<b>ANOMALÍA GPS COMPLEJA</b> (revisión manual)<br>{x.fecha.strftime(FORMATO_FECHA)}"
                 f"<br>{len(an.puntos_sospechosos)} punto(s) sospechoso(s) entre "
                 f"{an.punto_anterior.fecha.strftime('%H:%M:%S')} y {an.punto_siguiente.fecha.strftime('%H:%M:%S')}"
                 f"<br>Recorrido {an.distancia_recorrido_km:.2f} km vs. A-B {an.distancia_ab_km:.2f} km"
                 f"<br>" + "<br>".join(an.motivos))
        folium.Marker((x.latitud, x.longitud), icon=folium.Icon(color="red", icon="question-sign"),
                      popup=folium.Popup(texto, max_width=360), tooltip="Anomalía GPS compleja").add_to(capa)
        folium.PolyLine([(p.latitud, p.longitud) for p in (an.punto_anterior, *an.puntos_sospechosos,
                                                           an.punto_siguiente)],
                        color="#b71c1c", weight=3, dash_array="2 6").add_to(capa)


def generar_mapa(puntos: list[Punto], titulo: str, ruta_salida: Path,
                 geofencing: ResumenGeofencing | None = None, zonas: Zonas | None = None,
                 calidad: ResumenCalidadGps | None = None) -> Path:
    """Escribe el HTML en ruta_salida y devuelve esa ruta.

    puntos deben estar consolidados. geofencing y zonas son opcionales.
    """
    if not puntos:
        raise ValueError("No hay puntos para dibujar")

    tramos = calcular_tramos(puntos)
    trayectos = construir_trayectos(puntos, tramos)
    salidas_por_trayecto = asociar_con_trayectos(geofencing.eventos, trayectos) if geofencing else {}
    mapa = folium.Map(location=[puntos[0].latitud, puntos[0].longitud], zoom_start=12, control_scale=True)

    if zonas:
        _dibujar_zona_permitida(mapa, zonas)

    # Recorrido completo, tenue, para ver también lo que no es trayecto.
    folium.PolyLine([(p.latitud, p.longitud) for p in puntos],
                    color="#888888", weight=2, opacity=0.6).add_to(folium.FeatureGroup("Recorrido completo").add_to(mapa))

    capa_trayectos = folium.FeatureGroup("Trayectos").add_to(mapa)
    for i, t in enumerate(trayectos, start=1):
        color = COLORES_TRAYECTOS[(i - 1) % len(COLORES_TRAYECTOS)]
        texto = (f"<b>Trayecto {i}</b><br>{t.inicio.strftime(FORMATO_FECHA)} -> {t.fin.strftime(FORMATO_FECHA)}"
                 f"<br>Duración del trayecto: {_duracion(t.duracion)}<br>Puntos: {t.cantidad_puntos}"
                 f"<br>Distancia estimada observada: {t.distancia_estimada_observada_km:.2f} km")
        if geofencing:
            eventos = salidas_por_trayecto.get(i, [])
            if eventos:
                entidades = sorted({n for e in eventos for n in e.entidades})
                texto += f"<br><b>Salió de la zona permitida: sí</b> ({', '.join(entidades)})"
            else:
                texto += "<br>Salió de la zona permitida: no"
        folium.PolyLine([(p.latitud, p.longitud) for p in t.puntos], color=color, weight=4,
                        tooltip=f"Trayecto {i}", popup=folium.Popup(texto, max_width=300)).add_to(capa_trayectos)
        folium.CircleMarker(t.coordenada_inicial, radius=4, color=color, fill=True,
                            popup=f"Trayecto {i}: inicia {t.inicio.strftime(FORMATO_FECHA)}").add_to(capa_trayectos)
        folium.CircleMarker(t.coordenada_final, radius=4, color=color, fill=True,
                            popup=f"Trayecto {i}: termina {t.fin.strftime(FORMATO_FECHA)}").add_to(capa_trayectos)

    capa_paradas = folium.FeatureGroup("Paradas probables").add_to(mapa)
    capa_huecos = folium.FeatureGroup("Huecos con desplazamiento").add_to(mapa)
    for t in tramos:
        if t.tipo == PARADA_PROBABLE:
            folium.CircleMarker(
                t.coordenada_inicial, radius=6, color="#ff7f0e", fill=True, fill_opacity=0.8,
                popup=folium.Popup(
                    f"<b>Parada probable</b><br>{t.inicio.strftime(FORMATO_FECHA)} -> {t.fin.strftime(FORMATO_FECHA)}"
                    f"<br>Duración: {_duracion(t.duracion)}<br>Distancia entre extremos: {t.distancia_km:.3f} km",
                    max_width=300),
            ).add_to(capa_paradas)
        elif t.tipo == HUECO_CON_DESPLAZAMIENTO:
            folium.PolyLine(
                [t.coordenada_inicial, t.coordenada_final], color="#000000", weight=3, dash_array="8",
                popup=folium.Popup(
                    f"<b>Hueco con desplazamiento</b><br>{t.inicio.strftime(FORMATO_FECHA)} -> "
                    f"{t.fin.strftime(FORMATO_FECHA)}<br>Duración: {_duracion(t.duracion)}"
                    f"<br>Desplazamiento mínimo conocido (línea recta): {t.distancia_km:.2f} km"
                    f"<br>La trayectoria entre ambos puntos es desconocida",
                    max_width=300),
            ).add_to(capa_huecos)

    primero, ultimo = puntos[0], puntos[-1]
    folium.Marker((primero.latitud, primero.longitud), icon=folium.Icon(color="green", icon="play"),
                  popup=f"Punto inicial<br>{primero.fecha.strftime(FORMATO_FECHA)}").add_to(mapa)
    folium.Marker((ultimo.latitud, ultimo.longitud), icon=folium.Icon(color="red", icon="stop"),
                  popup=f"Punto final<br>{ultimo.fecha.strftime(FORMATO_FECHA)}").add_to(mapa)

    if geofencing:
        _dibujar_geofencing(mapa, geofencing)
    if calidad:
        _dibujar_calidad_gps(mapa, calidad)

    mapa.get_root().html.add_child(folium.Element(
        f"<div style='position:fixed;top:10px;left:50px;z-index:9999;background:white;padding:4px 8px;"
        f"border:1px solid #888;font:13px sans-serif'>{escape(titulo)}</div>"))
    folium.LayerControl().add_to(mapa)

    mapa.fit_bounds([(p.latitud, p.longitud) for p in puntos])
    ruta_salida.parent.mkdir(parents=True, exist_ok=True)
    mapa.save(str(ruta_salida))
    return ruta_salida
