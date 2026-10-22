"""Vigilancia de flota - interfaz operativa local (Streamlit).

    streamlit run app.py

Orquesta; no contiene lógica de análisis. Acciones SEPARADAS y explícitas:
  1. "Actualizar datos desde BAANOOL": adquisición (src/integracion_periodo.actualizar_desde_baanool).
  2. "Analizar periodo": análisis del periodo seleccionado con GPX/manifiestos YA descargados (de ese
     periodo exacto o de una adquisición mayor que lo contenga), sin tocar BAANOOL.
  3. Modo local: analiza las carpetas de entrada/ como siempre (no necesita token ni manifiesto).
Abrir la página, cambiar fechas o cambiar filtros NO consulta BAANOOL ni vuelve a analizar.
El token solo se lee de la variable de entorno (aquí únicamente se comprueba que exista); no se guarda.
Los resultados viven en st.session_state; nada se guarda en disco salvo lo que escribe la adquisición.
"""

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from src.geocercas import asociar_con_trayectos
from src.integracion_periodo import (NOTA_ESTADO_ACTUAL, actualizar_desde_baanool, advertencia_periodo,
                                     analizar_flota_local, aplicar_resultado_adquisicion, parsear_periodo,
                                     periodo_key, puede_generar_mapa, token_configurado)
from src.llaves import buscar_excel_en
from src.seleccion_periodo import (NOTA_SIN_CONSULTA, TIPO_EXACTA, analizar_periodo_local,
                                   aplicar_resultado_subperiodo, filas_movimiento_diario, texto_periodo)
from src.parametros import (DISTANCIA_MOVIMIENTO_ALTO_KM, DISTANCIA_MOVIMIENTO_BAJO_KM,
                            DISTANCIA_MOVIMIENTO_MUY_ALTO_KM,
                            PUNTOS_MINIMOS_FUERA, TOLERANCIA_FRONTERA_METROS)
from src.prioridades import (ALTA, CRITICA, DATOS_COBERTURA_LOCAL_INSUFICIENTE, DATOS_ERROR_ADQUISICION,
                             DATOS_SIN_DATOS_PERIODO, DATOS_SIN_DISPOSITIVO, DATOS_SIN_POSICIONES_SUBPERIODO, ESTADOS_DATOS, MOVIMIENTO_ALTO, NIVELES_MOVIMIENTO, NORMAL,
                             ORDEN_PRIORIDADES, REVISAR, filtrar_evaluaciones, generar_csv, resumir_flota)
from src.zonas import cargar_zonas

RAIZ = Path(__file__).resolve().parent
ICONOS = {CRITICA: "🔴", ALTA: "🟠", REVISAR: "🟡", MOVIMIENTO_ALTO: "🔵", NORMAL: "🟢"}
ETIQUETAS_PRIORIDAD = {CRITICA: "CRITICA", ALTA: "ALTA", REVISAR: "REVISAR",
                       MOVIMIENTO_ALTO: "MOVIMIENTO ALTO", NORMAL: "NORMAL"}

st.set_page_config(page_title="Vigilancia de flota", page_icon="🚚", layout="wide")


# ---------- utilidades de presentación ----------

def fmt_fecha(f: datetime | None) -> str:
    return f.strftime("%Y-%m-%d %H:%M") if f else "—"


def fmt_duracion(d: timedelta | None) -> str:
    if d is None:
        return "—"
    horas, resto = divmod(int(d.total_seconds()), 3600)
    return f"{horas} h {resto // 60:02d} min"


def si_no(valor: bool | None) -> str:
    return "—" if valor is None else ("Sí" if valor else "No")


def fuente_corta(e) -> str:
    """Procedencia resumida de la fila: adquisición exacta o el periodo de la adquisición mayor usada."""
    f = (st.session_state.get("fuentes_datos") or {}).get(e.resultado.llave_original)
    if f is None or not f.analizable:
        return "—"
    return "exacta" if f.exacta else f"{f.adquisicion_inicio:%d/%m %H:%M}→{f.adquisicion_fin:%d/%m %H:%M}"


def etiqueta_prioridad(p: str | None) -> str:
    """Sin datos no hay prioridad: se muestra "—" (el estado de datos va en su propia columna)."""
    return "—" if p is None else f"{ICONOS[p]} {ETIQUETAS_PRIORIDAD[p]}"


@st.cache_resource(show_spinner="Cargando zonas (INEGI)…")
def zonas_cacheadas(carpeta: str):
    """Se carga una sola vez por proceso. Si falta el archivo lanza FileNotFoundError (no se cachea)."""
    return cargar_zonas(Path(carpeta))


def obtener_zonas():
    try:
        return zonas_cacheadas(str(RAIZ / "zonas"))
    except FileNotFoundError as exc:
        st.session_state["aviso_zonas"] = str(exc)
        return None


def excel_por_defecto() -> str:
    try:
        return str(buscar_excel_en(RAIZ))
    except FileNotFoundError:
        return ""


# ---------- acciones (solo se ejecutan al pulsar su botón) ----------

def accion_actualizar_baanool(inicio: datetime, fin: datetime, entrada: Path, excel: Path) -> None:
    """ÚNICA acción que consulta BAANOOL. No analiza. Un fallo no destruye el dashboard."""
    barra = st.progress(0.0, text="Consultando dispositivos en BAANOOL…")
    ultimo = st.empty()

    def on_progreso(actual: int, total: int, llave: str, estado: str) -> None:
        barra.progress(actual / total if total else 1.0, text=f"Vehículo {actual} de {total}")
        ultimo.markdown(f"**{llave}**  \n{estado}")  # sin DNO, token ni URLs

    resultado = actualizar_desde_baanool(inicio, fin, entrada, excel, RAIZ, on_progreso=on_progreso)
    barra.empty()
    ultimo.empty()
    aplicar_resultado_adquisicion(st.session_state, resultado)


def accion_analizar_periodo(inicio: datetime, fin: datetime, excel: Path) -> None:
    """Analiza el periodo seleccionado con los GPX/manifiestos locales. NO consulta BAANOOL ni descarga,
    ni siquiera cuando faltan datos: eso es decisión del usuario con el otro botón."""
    if not excel.is_file():
        st.error(f"No encuentro el Excel de LLAVEs: {excel}")
        return
    st.session_state.pop("aviso_zonas", None)
    with st.spinner("Analizando periodo con archivos locales…"):
        resultado = analizar_periodo_local(inicio, fin, excel, RAIZ, obtener_zonas())
    if resultado.sin_cobertura:
        disponibles = resultado.intervalos_disponibles()
        st.warning(
            "COBERTURA_LOCAL_INSUFICIENTE: ningún manifiesto local contiene completamente el periodo solicitado "
            f"({texto_periodo(inicio, fin)}). Intervalos de adquisición disponibles: "
            + ("; ".join(disponibles) if disponibles else "ninguno")
            + ". No se descargó nada. Si quieres datos de este periodo, usa «Actualizar datos desde BAANOOL». "
              "Los resultados mostrados antes no cambiaron.")
        for aviso in resultado.avisos:
            st.caption(aviso)
        return
    resumen = resumir_flota(resultado.evaluaciones)
    aplicar_resultado_subperiodo(st.session_state, resultado, resumen, generar_csv(resultado.evaluaciones),
                                 datetime.now())
    st.session_state["info"]["geofencing"] = "aviso_zonas" not in st.session_state
    if resultado.llaves_fuera_del_catalogo:
        st.warning(f"Los manifiestos incluyen {len(resultado.llaves_fuera_del_catalogo)} LLAVE(s) que ya no están "
                   "en Lista_Vehiculos.xlsx; no se muestran.")


def accion_analizar_local(entrada: Path, excel: Path) -> None:
    """Modo local: como siempre; no necesita token, manifiesto ni BAANOOL."""
    if not entrada.is_dir():
        st.error(f"No existe la carpeta de entrada:\n{entrada}")
        return
    aviso_excel = None
    if not (excel and excel.is_file()):
        aviso_excel = "Sin Excel de LLAVES: las carpetas se muestran como [SIN LLAVE]."
    st.session_state.pop("aviso_zonas", None)
    with st.spinner("Analizando carpetas locales…"):
        evaluaciones = analizar_flota_local(entrada, excel, obtener_zonas())
    st.session_state["evaluaciones"] = evaluaciones
    st.session_state["resumen"] = resumir_flota(evaluaciones)
    st.session_state["csv"] = generar_csv(evaluaciones)
    st.session_state["info"] = {"modo": "LOCAL", "entrada": str(entrada), "hora": datetime.now(),
                                "aviso_excel": aviso_excel, "geofencing": "aviso_zonas" not in st.session_state}
    st.session_state["periodo_analizado"] = {"clave": None, "modo": "LOCAL", "inicio": None, "fin": None}
    st.session_state["manifiesto_usado"] = None
    st.session_state["origen_datos"] = None
    st.session_state["fuentes_datos"] = {}
    st.session_state["mapas"] = {}
    st.session_state["mapa_abierto"] = None


# ---------- barra lateral (parámetros descriptivos y archivos locales) ----------

with st.sidebar:
    st.header("Vigilancia de flota")
    st.subheader("Archivos locales")
    ruta_entrada = st.text_input("Carpeta de entrada", value=str(RAIZ / "entrada"), key="ruta_entrada")
    ruta_excel = st.text_input("Excel con la columna LLAVE", value=excel_por_defecto(), key="ruta_excel")
    st.divider()
    st.caption(
        f"Movimiento por día calendario: BAJO < {DISTANCIA_MOVIMIENTO_BAJO_KM} km · NORMAL "
        f"{DISTANCIA_MOVIMIENTO_BAJO_KM}–{DISTANCIA_MOVIMIENTO_ALTO_KM} km · ALTO > "
        f"{DISTANCIA_MOVIMIENTO_ALTO_KM} km · MUY ALTO > {DISTANCIA_MOVIMIENTO_MUY_ALTO_KM} km en un día; el "
        f"nivel del periodo es el máximo diario. Es descriptivo: BAJO no implica un problema y solo ALTO y "
        f"MUY ALTO generan la prioridad MOVIMIENTO ALTO (umbrales adaptados a plataformas de movilidad).\n\n"
        f"Frontera: tolerancia {TOLERANCIA_FRONTERA_METROS} m · salida confirmada con "
        f"{PUNTOS_MINIMOS_FUERA}+ puntos fuera consecutivos.\n\n"
        "Las distancias son estimaciones entre puntos GPX, no kilometraje real. "
        "Zona: Marco Geoestadístico INEGI (referencia operativa).\n\n" + NOTA_ESTADO_ACTUAL)

entrada_local = Path(ruta_entrada).expanduser()
excel_local = Path(ruta_excel).expanduser() if ruta_excel.strip() else None

st.title("Vigilancia de flota")

# ---------- periodo de trabajo ----------

st.header("Periodo de trabajo")
ayer = date.today() - timedelta(days=1)
c = st.columns(4)
fecha_inicial = c[0].date_input("Fecha inicial", value=ayer, key="p_fecha_ini")
hora_inicial = c[1].text_input("Hora inicial (HH:MM:SS)", value="00:00:00", key="p_hora_ini")
fecha_final = c[2].date_input("Fecha final", value=ayer, key="p_fecha_fin")
hora_final = c[3].text_input("Hora final (HH:MM:SS)", value="23:59:59", key="p_hora_fin")
st.caption("Horario: Ciudad de México (America/Mexico_City)")

periodo_valido = True
try:
    inicio_sel, fin_sel = parsear_periodo(fecha_inicial, hora_inicial, fecha_final, hora_final)
    clave_sel = periodo_key(inicio_sel, fin_sel)
except ValueError as exc:
    periodo_valido = False
    inicio_sel = fin_sel = clave_sel = None
    st.error(str(exc))

# ---------- adquisición BAANOOL ----------

st.header("Adquisición BAANOOL")
hay_token = token_configurado()
if hay_token:
    st.success("Token BAANOOL: Configurado")
else:
    st.warning("Token BAANOOL: No configurado. BAANOOL_TOKEN no está configurado. La descarga desde BAANOOL "
               "no está disponible, pero el análisis local continúa funcionando.")

if st.button("Actualizar datos desde BAANOOL", disabled=not (hay_token and periodo_valido),
             key="btn_actualizar", help="Consulta BAANOOL, descarga los GPX del periodo (secuencial) y "
                                        "actualiza el manifiesto. No analiza."):
    accion_actualizar_baanool(inicio_sel, fin_sel, entrada_local, excel_local or Path(excel_por_defecto()))

adq = st.session_state.get("adquisicion")
if adq:
    st.markdown(f"**Última adquisición** — periodo `{adq['clave_periodo']}`")
    if adq["codigo_error"] or not adq["ok"]:
        st.error(adq["mensaje"] or "La adquisición no se completó.")
    if adq["nota_manifiesto"]:
        st.warning(adq["nota_manifiesto"])
    r = adq["resumen"]
    if r:
        m = st.columns(6)
        m[0].metric("Dispositivos BAANOOL", r["dispositivos_baanool"])
        m[1].metric("Coincidencias exactas", r["coincidencias_exactas"])
        m[2].metric("Con GPX", r["con_gpx"])
        m[3].metric("Sin datos en el periodo", r["sin_datos_periodo"],
                    help="BAANOOL respondió pero no entregó posiciones GPS. No es un error.")
        m[4].metric("Solo local", r["solo_local"], help="LLAVE local sin dispositivo BAANOOL. No es un error.")
        m[5].metric("Errores", r["errores"] + r["sin_coordenadas"])
        if r["no_procesadas"]:
            st.caption(f"{r['no_procesadas']} vehículo(s) no se procesaron porque el lote se interrumpió.")
    if adq["ruta_manifiesto"]:
        st.caption(f"Manifiesto: `{adq['ruta_manifiesto']}` · lote completo: {si_no(adq['lote_completo'])}")
    if clave_sel and adq["clave_periodo"] != clave_sel:
        st.info("La última adquisición corresponde a otro periodo distinto del seleccionado.")

# ---------- análisis ----------

st.header("Análisis")
cols = st.columns([1, 1])
if cols[0].button("Analizar periodo", type="primary", disabled=not periodo_valido, key="btn_analizar_periodo",
                  help="Usa GPX ya descargados: de ese periodo exacto o de una adquisición mayor que lo contenga "
                       "(se filtran los puntos en memoria). No consulta BAANOOL ni descarga."):
    accion_analizar_periodo(inicio_sel, fin_sel, excel_local or Path(excel_por_defecto()))

with cols[1].expander("Modo local (sin BAANOOL)"):
    st.caption("Analiza todas las carpetas de entrada/ como siempre. No necesita token ni manifiesto y no "
               "asigna ningún periodo a los archivos.")
    if st.button("ANALIZAR FLOTA (modo local)", key="btn_analizar"):
        accion_analizar_local(entrada_local, excel_local)

if "evaluaciones" not in st.session_state:
    st.info("Aún no hay resultados. Usa «Analizar periodo» (requiere GPX descargados que cubran el periodo) o el modo local.")
    st.stop()

evaluaciones = st.session_state["evaluaciones"]
resumen = st.session_state["resumen"]
info = st.session_state["info"]
analizado = st.session_state.get("periodo_analizado") or {}
en_modo_periodo = analizado.get("modo") == "PERIODO"

if en_modo_periodo:
    st.markdown(f"**Los resultados mostrados corresponden al periodo:** {analizado['inicio']} → {analizado['fin']}")
    usado = st.session_state.get("manifiesto_usado") or {}
    origen = st.session_state.get("origen_datos")
    if origen:
        st.markdown("**ANÁLISIS LOCAL** — origen de datos: GPX descargados previamente")
        st.markdown(f"Periodo solicitado: **{analizado['inicio']} → {analizado['fin']}**")
        for fila in origen["resumen"]:
            etiqueta = ("Adquisición exacta" if fila["tipo"] == TIPO_EXACTA
                        else "Periodo de adquisición utilizado (mayor que el solicitado)")
            st.markdown(f"- {etiqueta}: **{texto_periodo(fila['adquisicion_inicio'], fila['adquisicion_fin'])}** · "
                        f"{fila['vehiculos']} vehículo(s) · manifiesto `{fila['manifiesto']}`")
        st.caption(NOTA_SIN_CONSULTA)
        if origen["usa_mayor"]:
            st.info("Parte de los datos proviene de una adquisición mayor: solo participan los puntos GPS dentro "
                    "del periodo solicitado. La distancia observada puede ser menor que la real si faltan puntos "
                    "cerca de los límites del intervalo o hay huecos de reporte.")
        if origen["avisos"]:
            with st.expander(f"Advertencias de selección de archivos ({len(origen['avisos'])})"):
                for a in origen["avisos"]:
                    st.markdown(f"- {a}")
    else:
        st.caption(f"Manifiesto `{usado.get('ruta')}` (ejecución {usado.get('fecha_ejecucion')}) · "
                   f"lote completo: {si_no(usado.get('lote_completo'))}"
                   + (f" · interrupción: {usado.get('motivo_interrupcion')}" if usado.get("motivo_interrupcion") else ""))
else:
    st.markdown("**Resultados del modo local** (todas las carpetas de entrada/; sin periodo asignado).")
aviso = advertencia_periodo(clave_sel, st.session_state) if clave_sel else None
if aviso:
    st.warning(aviso)

if st.session_state.get("aviso_zonas"):
    st.error("Geofencing desactivado: " + st.session_state["aviso_zonas"] +
             " Ningún vehículo con datos puede evaluarse contra la zona permitida.")
if info.get("aviso_excel"):
    st.warning(info["aviso_excel"])
st.caption(f"Análisis realizado {info['hora']:%Y-%m-%d %H:%M:%S} · los resultados permanecen en memoria hasta "
           "cerrar la sesión.")


# ---------- resumen general ----------

st.subheader("Análisis vehicular")
st.caption(f"Solo vehículos con datos analizables ({resumen.analizados} de {resumen.carpetas_encontradas} carpetas).")
v1 = st.columns(5)
v1[0].metric("🔴 Críticos", resumen.por_prioridad[CRITICA])
v1[1].metric("🟠 Altos", resumen.por_prioridad[ALTA])
v1[2].metric("🟡 Revisar", resumen.por_prioridad[REVISAR])
v1[3].metric("🔵 Movimiento alto", resumen.por_prioridad[MOVIMIENTO_ALTO])
v1[4].metric("🟢 Normales", resumen.por_prioridad[NORMAL])
v2 = st.columns(5)
v2[0].metric("Analizados", resumen.analizados)
v2[1].metric("Con salida confirmada", resumen.con_salida_confirmada)
v2[2].metric("Terminaron fuera", resumen.terminaron_fuera)
v2[3].metric("Nivel de movimiento ALTO/MUY ALTO", resumen.movimiento_alto)
v2[4].metric("Con huecos con desplazamiento", resumen.con_huecos)

st.subheader("Cobertura de datos")
if en_modo_periodo:
    st.caption("Adquisición del periodo (separada de las métricas operativas). SIN_DATOS_PERIODO y "
               "SIN_DISPOSITIVO_BAANOOL no son errores ni incidencias del vehículo.")
    d = st.columns(6)
    d[0].metric("Vehículos locales", resumen.carpetas_encontradas)
    d[1].metric("Con datos analizables", resumen.analizados)
    d[2].metric("Sin datos en el periodo", resumen.sin_datos_periodo)
    d[3].metric("Sin dispositivo BAANOOL", resumen.sin_dispositivo)
    d[4].metric("Errores de adquisición", resumen.errores_adquisicion)
    d[5].metric("Sin GPX / con error", resumen.sin_gpx + resumen.con_error)
    if resumen.sin_posiciones_subperiodo or resumen.cobertura_insuficiente:
        d = st.columns(6)
        d[0].metric("Sin posiciones en el subperiodo", resumen.sin_posiciones_subperiodo,
                    help="El archivo local existe, pero no tiene puntos GPS dentro del intervalo seleccionado.")
        d[1].metric("Cobertura local insuficiente", resumen.cobertura_insuficiente,
                    help="Ninguna adquisición local con esta LLAVE cubre completamente el intervalo.")
else:
    st.caption("Carpetas, no vehículos con incidencias: una carpeta sin GPX puede significar solo que "
               "falta cargar el archivo.")
    d = st.columns(5)
    d[0].metric("Carpetas encontradas", resumen.carpetas_encontradas)
    d[1].metric("Con datos válidos", resumen.con_datos_validos)
    d[2].metric("Parciales", resumen.parciales)
    d[3].metric("Sin GPX", resumen.sin_gpx)
    d[4].metric("Con error", resumen.con_error)

# ---------- filtros ----------

entidades_disponibles = sorted({n for e in evaluaciones for n in e.entidades_fuera})
estados_presentes = [s for s in ESTADOS_DATOS if any(e.estado_datos == s for e in evaluaciones)]
c = st.columns([1.3, 1.6, 1, 1, 1.4, 1.4])
solo_atencion = c[0].checkbox("Mostrar solo vehículos que requieren atención", value=True, key="f_atencion",
                              help="Incidencias del vehículo: CRITICA, ALTA, REVISAR y MOVIMIENTO ALTO. "
                                   "No incluye vehículos sin datos.")
problemas_datos = c[0].checkbox("Mostrar pendientes/problemas de datos", value=False, key="f_datos",
                                help="PARCIAL, SIN_DATOS_PERIODO, SIN_DISPOSITIVO_BAANOOL, ERROR_ADQUISICION, "
                                     "SIN_GPX y ERROR. Con ambas casillas se muestra la unión.")
prioridades = c[1].multiselect("Prioridad", ORDEN_PRIORIDADES, key="f_prioridad",
                               format_func=lambda p: ETIQUETAS_PRIORIDAD[p])
salida = c[2].selectbox("Salida", ["todos", "con", "sin"], key="f_salida",
                        format_func={"todos": "Todas", "con": "Con salida", "sin": "Sin salida"}.get)
termino = c[3].selectbox("Terminó fuera", ["todos", "si", "no"], key="f_termino",
                         format_func={"todos": "Todos", "si": "Sí", "no": "No"}.get)
entidades = c[4].multiselect("Entidad externa", entidades_disponibles, key="f_entidad")
niveles = c[5].multiselect("Nivel de movimiento", NIVELES_MOVIMIENTO, key="f_nivel")
c2 = st.columns([2, 2])
estados_datos = c2[0].multiselect("Estado de datos", estados_presentes, key="f_estado_datos")
texto_busqueda = c2[1].text_input("Buscar en la LLAVE", key="f_texto")

visibles = filtrar_evaluaciones(evaluaciones, prioridades=prioridades, salida=salida, termino_fuera=termino,
                                entidades=entidades, niveles=niveles, solo_atencion=solo_atencion,
                                problemas_datos=problemas_datos, estados_datos=estados_datos,
                                texto=texto_busqueda)

cabecera = st.columns([4, 1])
cabecera[0].subheader(f"Flota: {len(visibles)} de {len(evaluaciones)} vehículos")
cabecera[1].download_button(
    "Descargar resumen (CSV)", data=st.session_state["csv"].encode("utf-8-sig"),
    file_name=f"resumen_flota_{info['hora']:%Y%m%d_%H%M}.csv", mime="text/csv", width="stretch",
    key="btn_csv")

# ---------- tabla principal ----------

if not visibles:
    st.success("Ningún vehículo coincide con los filtros.")
else:
    tabla = pd.DataFrame([{
        "Prioridad": etiqueta_prioridad(e.prioridad),
        "LLAVE": e.identificador,
        "Distancia obs. (km)": e.distancia_observada_km,
        "Movimiento": e.nivel_movimiento or "—",
        "Trayectos": e.trayectos,
        "Salidas": e.salidas_confirmadas,
        "Terminó fuera": si_no(e.termino_fuera),
        "Entidades fuera": ", ".join(e.entidades_fuera),
        "Huecos": e.huecos_con_desplazamiento,
        "Conflictos": e.conflictos_timestamp,
        "Datos": e.estado_datos,
        "Adquisición": e.estado_adquisicion or "—",
        "Fuente": fuente_corta(e),
        "Estado actual BAANOOL": e.estado_actual_baanool or "—",
        "Calidad GPS": {"SIN_ALERTAS": "SIN ALERTAS", "REVISAR": "REVISAR"}.get(e.calidad_gps, "—"),
        "Motivo": " | ".join(e.motivos),
    } for e in visibles])
    seleccion = st.dataframe(
        tabla, hide_index=True, width="stretch", height=min(40 + 35 * len(tabla), 420),
        on_select="rerun", selection_mode="single-row", key="tabla_flota",
        column_config={"Distancia obs. (km)": st.column_config.NumberColumn(format="%.1f"),
                       "Motivo": st.column_config.TextColumn(width="large")})
    st.caption("Selecciona una fila para ver el detalle. “—” significa sin datos (no es lo mismo que 0).")

    filas = seleccion.selection.rows
    elegido = visibles[filas[0]] if filas else visibles[0]

    # ---------- detalle del vehículo ----------

    st.divider()
    v = elegido.resultado
    st.header(f"{ICONOS.get(elegido.prioridad, '⚪')} {elegido.identificador}")
    prioridad_texto = ("sin determinar (no hay datos)" if elegido.prioridad is None
                       else ETIQUETAS_PRIORIDAD[elegido.prioridad])
    st.markdown(f"**Prioridad: {prioridad_texto}** · Estado de datos: **`{elegido.estado_datos}`**"
                + (f" · Estado de adquisición: **`{elegido.estado_adquisicion}`**"
                   if elegido.estado_adquisicion else "")
                + (f" · Estado actual BAANOOL: **{elegido.estado_actual_baanool}**"
                   if elegido.estado_actual_baanool else "")
                + f" · Carpeta: `{v.nombre_carpeta}` · Archivos GPX: {v.numero_gpx}")
    if elegido.estado_actual_baanool:
        st.caption(NOTA_ESTADO_ACTUAL)
    if elegido.inconsistencia:
        st.warning(f"Inconsistencia entre el manifiesto y los archivos: {elegido.inconsistencia}")
    for motivo in elegido.motivos:
        st.markdown(f"- {motivo}")
    fuente = (st.session_state.get("fuentes_datos") or {}).get(v.llave_original)
    if fuente is not None and fuente.manifiesto:
        st.caption(
            f"Origen de datos: adquisición {'exacta' if fuente.exacta else 'mayor'} "
            f"{texto_periodo(fuente.adquisicion_inicio, fuente.adquisicion_fin)} · manifiesto "
            f"`{fuente.manifiesto}`" + (f" · archivo `{fuente.archivo_relativo}`" if fuente.archivo else ""))
        for aviso in fuente.advertencias:
            st.warning(aviso)

    if elegido.errores:
        st.subheader("Errores de archivo")
        st.dataframe(pd.DataFrame(elegido.errores, columns=["Archivo", "Error"]), hide_index=True, width="stretch")

    if not elegido.tiene_datos:
        explicaciones = {
            DATOS_SIN_DATOS_PERIODO: "BAANOOL no entregó posiciones GPS para el periodo seleccionado.",
            DATOS_SIN_DISPOSITIVO: "Esta LLAVE local no tiene un dispositivo BAANOOL asociado actualmente.",
            DATOS_SIN_POSICIONES_SUBPERIODO: "No se encontraron posiciones GPS dentro del intervalo seleccionado "
                                             "en los archivos locales disponibles.",
            DATOS_COBERTURA_LOCAL_INSUFICIENTE: "Ningún archivo local cubre completamente el intervalo para esta "
                                                "LLAVE. Puedes actualizar desde BAANOOL con el otro botón.",
            DATOS_ERROR_ADQUISICION: "La adquisición de este vehículo no se completó; no hay datos que analizar.",
        }
        st.info(explicaciones.get(elegido.estado_datos, "Este vehículo no tiene datos utilizables.")
                + " No se calculan métricas ni prioridad (sin datos no equivale a sin movimiento).")
    else:
        r, a, g = v.resumen, v.actividad, v.geofencing

        st.subheader("Resumen")
        m = st.columns(5)
        m[0].metric("Distancia estimada observada", f"{r.distancia_estimada_observada_km:.1f} km")
        m[1].metric("Trayectos", len(a.trayectos))
        m[2].metric("Paradas probables", a.cantidad_paradas_probables)
        m[3].metric("Huecos con desplazamiento", a.cantidad_huecos_con_desplazamiento)
        m[4].metric("Nivel de movimiento (máx. diario)", elegido.nivel_movimiento)
        st.markdown(
            f"- Distancia máxima diaria: **{elegido.distancia_maxima_diaria_km:.1f} km** el "
            f"**{elegido.fecha_distancia_maxima:%Y-%m-%d}**\n"
            f"- Periodo de los datos: **{fmt_fecha(r.fecha_inicial)}** → **{fmt_fecha(r.fecha_final)}** "
            f"({r.cantidad_puntos} puntos)\n"
            f"- Actividad detectada: **{fmt_fecha(a.primera_actividad)}** → **{fmt_fecha(a.ultima_actividad)}**\n"
            f"- Ruido durante paradas: {r.distancia_ruido_paradas_km:.2f} km (no se suma)\n"
            f"- Desplazamiento mínimo en huecos: {r.desplazamiento_minimo_huecos_km:.2f} km "
            f"(línea recta; trayectoria desconocida, no se suma)\n"
            f"- Tiempo en paradas probables: {fmt_duracion(a.tiempo_en_paradas_probables)}")

        st.subheader("Movimiento por día")
        if en_modo_periodo:
            filas_dia = filas_movimiento_diario(elegido, datetime.strptime(analizado["inicio"], "%Y-%m-%d %H:%M:%S"),
                                                datetime.strptime(analizado["fin"], "%Y-%m-%d %H:%M:%S"))
            st.dataframe(pd.DataFrame([{
                "Fecha": f"{f['fecha']:%Y-%m-%d}", "Puntos GPS": f["puntos"],
                "Distancia observada (km)": None if f["distancia_km"] is None else round(f["distancia_km"], 1),
                "Nivel": f["nivel"] or "—", "Observación": f["observacion"],
            } for f in filas_dia]), hide_index=True, width="stretch")
        else:
            st.dataframe(pd.DataFrame([{
                "Fecha": f"{d:%Y-%m-%d}",
                "Distancia observada (km)": round(km, 1),
                "Nivel": elegido.nivel_por_dia[d],
            } for d, km in elegido.distancia_por_dia.items()]), hide_index=True, width="stretch")
        st.caption("Día calendario en America/Mexico_City. Un tramo que cruza la medianoche se reparte "
                   "proporcionalmente al tiempo en cada día. El nivel del periodo es el máximo diario. "
                   "Un día sin posiciones no es lo mismo que 0 km observados.")

        if g is None:
            st.warning("Geofencing no disponible para este análisis.")
        else:
            st.subheader("Geofencing")
            m = st.columns(6)
            m[0].metric("Puntos dentro", g.puntos_dentro)
            m[1].metric("Tolerancia", g.puntos_tolerancia)
            m[2].metric("Fuera", g.puntos_fuera)
            m[3].metric("Salidas confirmadas", g.salidas_confirmadas)
            m[4].metric("No confirmadas", g.salidas_no_confirmadas)
            m[5].metric("Terminó fuera", si_no(elegido.termino_fuera))
            st.markdown(f"Entidades externas: **{', '.join(g.entidades_fuera) or 'ninguna'}**  \n"
                        f"Último punto geográfico fuera de la zona (diagnóstico): "
                        f"**{si_no(elegido.ultimo_punto_fuera)}**"
                        + ("" if elegido.termino_fuera or not elegido.ultimo_punto_fuera
                           else " — no alcanza los puntos mínimos para confirmar una salida"))

            st.subheader("Eventos de salida")
            if not g.eventos:
                st.success("SIN SALIDAS DE LA ZONA PERMITIDA")
            else:
                def estado_evento(e):
                    if e.inicia_fuera and e.confirmado:
                        base = "Inicio del periodo fuera de la zona"
                    elif e.confirmado:
                        base = "Salida confirmada"
                    else:
                        return "Salida no confirmada (diagnóstico)"
                    return base + (" · abierta" if e.abierta else "")

                st.dataframe(pd.DataFrame([{
                    "Estado": estado_evento(e),
                    "Último punto dentro": fmt_fecha(e.ultimo_punto_dentro.punto.fecha) if e.ultimo_punto_dentro else "— (el periodo empieza fuera)",
                    "Primer punto fuera": fmt_fecha(e.primer_punto_fuera.punto.fecha),
                    "Último punto fuera": fmt_fecha(e.ultimo_punto_fuera.punto.fecha),
                    "Primer punto nuevamente dentro": fmt_fecha(e.primer_punto_dentro_posterior.punto.fecha)
                    if e.primer_punto_dentro_posterior else "Sin regreso observado",
                    "Entidades": ", ".join(e.entidades),
                    "Puntos fuera": e.cantidad_puntos_fuera,
                    "Duración observable fuera": fmt_duracion(e.duracion_observable_fuera),
                    "Información incompleta": "Sí: hueco con desplazamiento, trayectoria desconocida"
                    if (e.salida_con_hueco or e.regreso_con_hueco or e.huecos_durante_evento) else "",
                } for e in g.eventos]), hide_index=True, width="stretch")
                st.caption("La salida ocurrió en algún momento entre el último punto dentro y el primer punto fuera "
                           "(igual para el regreso): no se conoce la hora exacta de cruce. “Duración observable” "
                           "es solo entre puntos observados fuera, no la duración real fuera de la zona.")

        q = v.calidad_gps
        if q is not None:
            st.subheader("Calidad GPS")
            st.markdown(f"**Estado: {'REVISAR' if q.hay_alertas else 'SIN ALERTAS'}** — diagnóstico "
                        "independiente de la prioridad y del estado de datos; no modifica las métricas oficiales.")
            m = st.columns(4)
            m[0].metric("Velocidad máxima estimada",
                        "—" if q.velocidad_maxima_kmh is None else f"{q.velocidad_maxima_kmh:.0f} km/h")
            m[1].metric("Tramos velocidad sospechosa", len(q.tramos_velocidad_sospechosa))
            m[2].metric("Saltos GPS probables", len(q.saltos_probables))
            m[3].metric("Anomalías GPS complejas", len(q.anomalias_complejas))
            m = st.columns(4)
            m[0].metric("Distancia oficial", f"{q.distancia_oficial_km:.1f} km")
            m[1].metric("Distancia depurada (experimental)", f"{q.distancia_depurada_km:.1f} km")
            m[2].metric("Diferencia por saltos", f"{q.distancia_atribuida_saltos_km:.2f} km")
            m[3].metric("Diferencia porcentual", f"{q.diferencia_porcentual:.2f} %")
            if q.saltos_probables:
                st.caption("La distancia depurada es EXPERIMENTAL: reemplaza A→X + X→B por A→B solo en saltos "
                           "aislados de alta confianza. Prioridad, geofencing, movimiento y distancia "
                           "oficiales usan los datos originales.")
                if (q.geofencing_depurado is not None and g is not None and
                        (q.geofencing_depurado.puntos_fuera != g.puntos_fuera or
                         q.geofencing_depurado.salidas_confirmadas != g.salidas_confirmadas)):
                    st.warning(
                        f"EXPERIMENTAL: sin los saltos probables, el geofencing daría {q.geofencing_depurado.puntos_fuera} "
                        f"puntos fuera y {q.geofencing_depurado.salidas_confirmadas} salidas confirmadas "
                        f"(oficial: {g.puntos_fuera} y {g.salidas_confirmadas}). El oficial no cambia.")
                st.dataframe(pd.DataFrame([{
                    "Fecha/hora (X)": s.punto_sospechoso.fecha.strftime("%Y-%m-%d %H:%M:%S"),
                    "Coordenada original": f"{s.punto_sospechoso.latitud:.5f}, {s.punto_sospechoso.longitud:.5f}",
                    "A-X (km)": round(s.distancia_ax_km, 2), "X-B (km)": round(s.distancia_xb_km, 2),
                    "A-B (km)": round(s.distancia_ab_km, 2),
                    "A-X (s)": round(s.duracion_ax_segundos), "X-B (s)": round(s.duracion_xb_segundos),
                    "Vel. A-X": round(s.velocidad_ax_kmh), "Vel. X-B": round(s.velocidad_xb_kmh),
                    "Relación desvío": "∞" if s.relacion_desvio == float("inf") else round(s.relacion_desvio, 1),
                    "Atribuido (km)": round(s.distancia_atribuida_km, 2),
                } for s in q.saltos_probables]), hide_index=True, width="stretch")
            if q.anomalias_complejas:
                st.warning(f"{len(q.anomalias_complejas)} anomalía(s) GPS compleja(s): revisión manual; "
                           "no se corrigen ni se excluyen.")
                st.dataframe(pd.DataFrame([{
                    "Desde": an.punto_anterior.fecha.strftime("%Y-%m-%d %H:%M:%S"),
                    "Hasta": an.punto_siguiente.fecha.strftime("%Y-%m-%d %H:%M:%S"),
                    "Puntos sospechosos": len(an.puntos_sospechosos),
                    "Recorrido (km)": round(an.distancia_recorrido_km, 2),
                    "A-B (km)": round(an.distancia_ab_km, 2),
                    "Motivos": " | ".join(an.motivos),
                } for an in q.anomalias_complejas]), hide_index=True, width="stretch")
            if q.tramos_velocidad_sospechosa:
                with st.expander(f"Tramos con velocidad sospechosa ({len(q.tramos_velocidad_sospechosa)})"):
                    st.dataframe(pd.DataFrame([{
                        "Inicio": t.punto_inicial.fecha.strftime("%Y-%m-%d %H:%M:%S"),
                        "Fin": t.punto_final.fecha.strftime("%H:%M:%S"),
                        "Duración (s)": round(t.duracion_segundos),
                        "Distancia (km)": round(t.distancia_km, 2),
                        "Velocidad (km/h)": round(t.velocidad_estimada_kmh),
                        "Relacionado con": t.relacionado_con or "solo velocidad",
                    } for t in q.tramos_velocidad_sospechosa]), hide_index=True, width="stretch")
                    st.caption("Velocidad sospechosa no implica posición errónea: el tramo se conserva por completo.")

        st.subheader("Trayectos")
        if not a.trayectos:
            st.write("Sin trayectos detectados (movimiento menor al mínimo configurado).")
        else:
            salidas = asociar_con_trayectos(g.eventos, a.trayectos) if g else {}
            st.dataframe(pd.DataFrame([{
                "#": i,
                "Inicio": fmt_fecha(t.inicio),
                "Fin": fmt_fecha(t.fin),
                "Duración": fmt_duracion(t.duracion),
                "Puntos": t.cantidad_puntos,
                "Distancia obs. (km)": round(t.distancia_estimada_observada_km, 1),
                "Salida de zona": ("Sí (" + ", ".join(sorted({n for e in salidas[i] for n in e.entidades})) + ")")
                if i in salidas else ("No" if g else "—"),
            } for i, t in enumerate(a.trayectos, start=1)]), hide_index=True, width="stretch")
            st.caption("La duración es el periodo de actividad reportado, no tiempo en movimiento.")

        # ---------- mapa (perezoso: solo el vehículo que se abre) ----------

        st.subheader("Mapa")
        clave_mapa = str(v.carpeta)
        if not puede_generar_mapa(elegido):
            st.caption("No hay posiciones que mostrar en el mapa.")
        elif st.button("Mostrar mapa de este vehículo", key=f"btn_mapa_{clave_mapa}"):
            st.session_state["mapa_abierto"] = clave_mapa
        if puede_generar_mapa(elegido) and st.session_state.get("mapa_abierto") == clave_mapa:
            mapas = st.session_state["mapas"]
            if clave_mapa not in mapas:
                from src.mapa import generar_mapa  # Folium solo se importa al abrir un mapa

                with st.spinner("Generando mapa…"), tempfile.TemporaryDirectory() as tmp:
                    ruta = generar_mapa(v.puntos, elegido.identificador, Path(tmp) / "mapa.html", g,
                                        obtener_zonas(), v.calidad_gps)
                    mapas[clave_mapa] = ruta.read_text(encoding="utf-8")
            # HTML generado localmente por src/mapa.py (no es contenido externo).
            st.iframe(mapas[clave_mapa], height=680)

# ---------- errores de archivo de toda la flota ----------

errores = [(e.identificador, archivo, error) for e in evaluaciones for archivo, error in e.errores]
if errores:
    with st.expander(f"Errores de archivos en la flota ({len(errores)})"):
        st.dataframe(pd.DataFrame(errores, columns=["Vehículo", "Archivo", "Error"]),
                     hide_index=True, width="stretch")
