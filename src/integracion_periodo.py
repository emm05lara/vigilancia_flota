"""Integración operativa BAANOOL -> MANIFIESTO -> ANÁLISIS (etapa 7). Sin Streamlit.

Dos rutas EXPLÍCITAS y separadas:

A) Modo por PERIODO BAANOOL: el manifiesto del periodo exacto es la fuente de verdad de adquisición.
   Solo se analiza el `archivo_gpx` que el manifiesto indica; nunca se hace glob de *.gpx, así que un
   periodo no se mezcla con GPX de otros periodos ni con archivos manuales. Cada LLAVE local del
   catálogo produce un registro, tenga o no GPX.
B) Modo local: `analizar_flota_local` conserva el análisis de las carpetas de entrada/ tal como era;
   no necesita token ni BAANOOL.

La adquisición (`actualizar_desde_baanool`) y el análisis (`analizar_periodo`) son acciones distintas.
Reutiliza descarga_lote, manifiesto, analizador, prioridades, calidad GPS, movimiento y geocercas
sin copiar su lógica. No guarda el token ni DNO; nada de esto llama a BAANOOL salvo
`actualizar_desde_baanool`, que se invoca explícitamente.
"""

import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable, MutableMapping

from crear_carpetas_llaves import limpiar_nombre_carpeta

from .analizador import ResultadoVehiculo, analizar_archivos, analizar_flota
from .baanool import (ERROR_RED, RATE_LIMIT_BAANOOL, TIMEOUT, TOKEN_AUSENTE, TOKEN_INVALIDO, VARIABLE_TOKEN,
                      ErrorBaanool, crear_sesion, obtener_token)
from .descarga_lote import (CARPETA_AMBIGUA, DESCARGADO, ERROR_DESCARGA, ERROR_EXPORTACION, GPX_INVALIDO,
                            LOTE_COMPLETADO, LOTE_DETENIDO_RATE_LIMIT, LOTE_DETENIDO_TOKEN, SIN_COORDENADAS,
                            SIN_DATOS_PERIODO, YA_EXISTE, ejecutar_lote, nombre_archivo_lote, preparar_mapeo)
from .llaves import cargar_llaves, indexar_por_carpeta
from .manifiesto import (CARPETA_MANIFIESTOS, NO_PROCESADO, ErrorEscrituraManifiesto, actualizar_manifiesto,
                         cargar_manifiesto_adquisicion, estados_por_llave, nombre_manifiesto)
from .mapeo_baanool import CONFLICTO, POSIBLE_COINCIDENCIA, SOLO_LOCAL
from .prioridades import (DATOS_ERROR_ADQUISICION, DATOS_SIN_DATOS_PERIODO, DATOS_SIN_DISPOSITIVO,
                          DATOS_SIN_GPX, EvaluacionVehiculo, evaluacion_sin_datos, evaluar_flota,
                          evaluar_vehiculo, ordenar_evaluaciones)
from .zonas import Zonas

ANALIZABLE = "ANALIZABLE"  # marcador interno: el vehículo tiene un GPX del periodo que sí se analiza

ESTADOS_ADQUISICION_CON_ARCHIVO = (DESCARGADO, YA_EXISTE)
ESTADOS_ADQUISICION_CON_ERROR = (ERROR_EXPORTACION, ERROR_DESCARGA, GPX_INVALIDO, SIN_COORDENADAS, CARPETA_AMBIGUA)
ESTADOS_SIN_DISPOSITIVO = (SOLO_LOCAL, POSIBLE_COINCIDENCIA, CONFLICTO)

NOTA_ESTADO_ACTUAL = ("El estado actual de BAANOOL (ONLINE/OFFLINE) es una fotografía del momento de la "
                      "consulta y no describe necesariamente el periodo analizado.")
MENSAJE_SIN_DATOS_PERIODO = "BAANOOL no entregó posiciones GPS para el periodo seleccionado."
MENSAJE_SIN_DISPOSITIVO = "Esta LLAVE local no tiene un dispositivo BAANOOL asociado actualmente."


# ---------- periodo ----------

def parsear_periodo(fecha_inicio: date, hora_inicio: str, fecha_fin: date, hora_fin: str
                    ) -> tuple[datetime, datetime]:
    """Fecha + 'HH:MM:SS' -> (inicio, fin) en hora local. Lanza ValueError con un mensaje legible.

    No convierte zonas horarias: eso lo hace src/baanool.py al pedir a BAANOOL.
    """
    def combinar(fecha: date, texto: str, etiqueta: str) -> datetime:
        try:
            hora = datetime.strptime(texto.strip(), "%H:%M:%S").time()
        except ValueError:
            raise ValueError(f"{etiqueta}: usa el formato HH:MM:SS") from None
        return datetime.combine(fecha, hora)

    inicio = combinar(fecha_inicio, hora_inicio, "Hora inicial")
    fin = combinar(fecha_fin, hora_fin, "Hora final")
    if inicio >= fin:
        raise ValueError("El inicio del periodo debe ser anterior al fin")
    return inicio, fin


def periodo_key(inicio: datetime, fin: datetime) -> str:
    """Identificador determinista del periodo (hora local): 2026-10-10_000000__2026-10-10_235959."""
    return nombre_manifiesto(inicio, fin).removesuffix(".json")


def token_configurado() -> bool:
    """Solo comprueba la presencia de BAANOOL_TOKEN; nunca lee ni expone su valor."""
    return bool(os.environ.get(VARIABLE_TOKEN, "").strip())


# ---------- rutas del manifiesto ----------

def resolver_ruta_gpx(ruta_relativa, raiz: Path) -> tuple[Path | None, str | None]:
    """Resuelve `archivo_gpx` contra la raíz del proyecto. Devuelve (ruta, None) o (None, problema).

    Rechaza rutas absolutas, con unidad de disco, con '..' que escapen de la raíz y archivos que no
    sean .gpx: un manifiesto manipulado no puede hacer leer archivos arbitrarios.
    """
    if not isinstance(ruta_relativa, str) or not ruta_relativa.strip():
        return None, "El manifiesto no indica el archivo GPX"
    texto = ruta_relativa.strip()
    if texto.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", texto):
        return None, "Ruta insegura en el manifiesto: es absoluta, y debería ser relativa al proyecto"
    base = raiz.resolve()
    candidato = (base / texto).resolve()
    if not candidato.is_relative_to(base):
        return None, "Ruta insegura en el manifiesto: sale de la raíz del proyecto"
    if candidato.suffix.lower() != ".gpx":
        return None, "Ruta insegura en el manifiesto: no es un archivo .gpx"
    return candidato, None


# ---------- contexto por LLAVE ----------

@dataclass(frozen=True)
class ContextoVehiculoPeriodo:
    llave: str
    nombre_carpeta: str
    estado_datos: str  # ANALIZABLE o uno de los estados sin datos de prioridades.py
    estado_adquisicion: str | None  # tal como lo registra el manifiesto; None si la LLAVE no está en él
    estado_actual_baanool: str | None  # fotografía; no interviene en ninguna decisión
    archivo: Path | None  # solo si ANALIZABLE
    mensaje: str | None = None
    inconsistencia: str | None = None

    @property
    def analizable(self) -> bool:
        return self.estado_datos == ANALIZABLE


def _clasificar(entrada: dict | None, raiz: Path, nombre_esperado: str | None
                ) -> tuple[str, Path | None, str | None, str | None]:
    """(estado_datos, archivo, mensaje, inconsistencia) para una entrada del manifiesto."""
    if entrada is None:
        return (DATOS_SIN_GPX, None, "El manifiesto del periodo no incluye esta LLAVE",
                "LLAVE ausente del manifiesto")
    estado = entrada.get("estado_adquisicion")
    mensaje = entrada.get("mensaje")

    if estado in ESTADOS_ADQUISICION_CON_ARCHIVO:
        ruta, problema = resolver_ruta_gpx(entrada.get("archivo_gpx"), raiz)
        if problema is None and nombre_esperado and ruta.name != nombre_esperado:
            problema = "El archivo del manifiesto no corresponde al periodo seleccionado"
        if problema is None and not ruta.is_file():
            problema = (f"El manifiesto indica {estado} pero el archivo referenciado ya no existe "
                        "(inconsistencia entre manifiesto y archivos)")
        if problema:
            return DATOS_SIN_GPX, None, problema, problema
        return ANALIZABLE, ruta, mensaje, None
    if estado == SIN_DATOS_PERIODO:
        return DATOS_SIN_DATOS_PERIODO, None, MENSAJE_SIN_DATOS_PERIODO, None
    if estado in ESTADOS_SIN_DISPOSITIVO:
        extra = f" ({mensaje})" if mensaje and estado != SOLO_LOCAL else ""
        return DATOS_SIN_DISPOSITIVO, None, MENSAJE_SIN_DISPOSITIVO + extra, None
    if estado in ESTADOS_ADQUISICION_CON_ERROR:
        return DATOS_ERROR_ADQUISICION, None, f"Problema de adquisición ({estado}): {mensaje or 'sin detalle'}", None
    if estado == NO_PROCESADO:
        return DATOS_SIN_GPX, None, "No se adquirió en la última corrida (lote incompleto)", None
    return DATOS_ERROR_ADQUISICION, None, f"Estado de adquisición no reconocido: {estado!r}", None


def construir_contextos(llaves: list[str], manifiesto: dict, raiz: Path) -> list[ContextoVehiculoPeriodo]:
    """Un registro por LLAVE del catálogo local (no solo las que tienen GPX)."""
    entradas = estados_por_llave(manifiesto)
    try:
        nombre_esperado = nombre_archivo_lote(
            datetime.strptime(manifiesto["periodo_inicio_local"], "%Y-%m-%d %H:%M:%S"),
            datetime.strptime(manifiesto["periodo_fin_local"], "%Y-%m-%d %H:%M:%S"))
    except (KeyError, ValueError):
        nombre_esperado = None

    contextos = []
    for llave in dict.fromkeys(l.strip() for l in llaves if l and l.strip()):
        entrada = entradas.get(llave)
        estado_datos, archivo, mensaje, inconsistencia = _clasificar(entrada, raiz, nombre_esperado)
        contextos.append(ContextoVehiculoPeriodo(
            llave=llave, nombre_carpeta=limpiar_nombre_carpeta(llave), estado_datos=estado_datos,
            estado_adquisicion=entrada.get("estado_adquisicion") if entrada else None,
            estado_actual_baanool=entrada.get("estado_actual_baanool") if entrada else None,
            archivo=archivo, mensaje=mensaje, inconsistencia=inconsistencia))
    return contextos


# ---------- análisis por periodo (ruta A) ----------

def evaluar_contextos(contextos: list[ContextoVehiculoPeriodo], raiz: Path, zonas: Zonas | None = None
                      ) -> list[EvaluacionVehiculo]:
    """Analiza SOLO el archivo de cada contexto analizable; el resto recibe un estado sin datos.

    Los GPX válidos pasan por exactamente el análisis existente (consolidación, movimiento, geocercas,
    calidad GPS, prioridad). Los demás no reciben prioridad, métricas ni calidad GPS.
    """
    evaluaciones = []
    for c in contextos:
        if c.analizable:
            v = analizar_archivos(c.archivo.parent, [c.archivo], c.llave, zonas)
            ev = evaluar_vehiculo(v)
            evaluaciones.append(replace(ev, estado_adquisicion=c.estado_adquisicion,
                                        estado_actual_baanool=c.estado_actual_baanool))
        else:
            v = ResultadoVehiculo(carpeta=raiz / "entrada" / c.nombre_carpeta, nombre_carpeta=c.nombre_carpeta,
                                  llave_original=c.llave)
            evaluaciones.append(evaluacion_sin_datos(
                v, c.estado_datos, (c.mensaje,) if c.mensaje else (),
                estado_adquisicion=c.estado_adquisicion, estado_actual_baanool=c.estado_actual_baanool,
                inconsistencia=c.inconsistencia))
    return ordenar_evaluaciones(evaluaciones)


@dataclass
class ResultadoAnalisisPeriodo:
    evaluaciones: list[EvaluacionVehiculo]
    clave_periodo: str
    inicio: datetime
    fin: datetime
    ruta_manifiesto: str  # relativa al proyecto
    fecha_ejecucion_manifiesto: str | None
    lote_completo: bool | None
    motivo_interrupcion: str | None
    llaves_fuera_del_catalogo: list[str] = field(default_factory=list)  # en el manifiesto pero no en el Excel

    @property
    def inconsistencias(self) -> list[EvaluacionVehiculo]:
        return [e for e in self.evaluaciones if e.inconsistencia]


def analizar_periodo(inicio: datetime, fin: datetime, archivo_excel: Path, raiz: Path,
                     zonas: Zonas | None = None, carpeta_manifiestos: Path | None = None
                     ) -> ResultadoAnalisisPeriodo | None:
    """Analiza el periodo EXACTO usando su manifiesto. Devuelve None si no existe manifiesto (no se
    inventan estados de adquisición). No consulta BAANOOL ni descarga nada."""
    manifiesto = cargar_manifiesto_adquisicion(inicio, fin, carpeta_manifiestos)  # None | ManifiestoInvalido
    if manifiesto is None:
        return None
    llaves = cargar_llaves(archivo_excel)
    contextos = construir_contextos(llaves, manifiesto, raiz)
    evaluaciones = evaluar_contextos(contextos, raiz, zonas)
    catalogo = set(llaves)
    carpeta = carpeta_manifiestos or CARPETA_MANIFIESTOS
    try:
        relativa = (carpeta / nombre_manifiesto(inicio, fin)).resolve().relative_to(raiz.resolve()).as_posix()
    except ValueError:
        relativa = f"{carpeta.name}/{nombre_manifiesto(inicio, fin)}"
    return ResultadoAnalisisPeriodo(
        evaluaciones=evaluaciones, clave_periodo=periodo_key(inicio, fin), inicio=inicio, fin=fin,
        ruta_manifiesto=relativa, fecha_ejecucion_manifiesto=manifiesto.get("fecha_ejecucion"),
        lote_completo=manifiesto.get("lote_completo"), motivo_interrupcion=manifiesto.get("motivo_interrupcion"),
        llaves_fuera_del_catalogo=sorted(l for l in estados_por_llave(manifiesto) if l not in catalogo))


# ---------- modo local (ruta B) ----------

def analizar_flota_local(carpeta_entrada: Path, archivo_excel: Path | None, zonas: Zonas | None = None
                         ) -> list[EvaluacionVehiculo]:
    """El análisis local de siempre: todas las carpetas de entrada/, sin BAANOOL ni manifiesto."""
    llaves = cargar_llaves(archivo_excel) if archivo_excel and archivo_excel.is_file() else []
    return evaluar_flota(analizar_flota(carpeta_entrada, indexar_por_carpeta(llaves), zonas))


# ---------- adquisición (única parte que consulta BAANOOL) ----------

@dataclass(frozen=True)
class ResultadoAdquisicion:
    """Solo datos NO sensibles: sin token, headers, DNO ni URLs."""
    clave_periodo: str
    ok: bool  # el lote terminó completo
    codigo_error: str | None  # TOKEN_AUSENTE / TOKEN_INVALIDO / RATE_LIMIT_BAANOOL / ERROR_RED ...
    mensaje: str | None
    resumen: dict[str, int]
    ruta_manifiesto: str | None
    lote_completo: bool
    motivo_interrupcion: str | None
    nota_manifiesto: str | None = None


def mensaje_para_usuario(codigo: str | None, detalle: str | None = None) -> str:
    textos = {
        TOKEN_AUSENTE: "BAANOOL_TOKEN no está configurado. La descarga desde BAANOOL no está disponible, "
                       "pero el análisis local continúa funcionando.",
        TOKEN_INVALIDO: "BAANOOL rechazó el token: la sesión debe renovarse. Define un token nuevo en "
                        "BAANOOL_TOKEN y vuelve a intentar.",
        RATE_LIMIT_BAANOOL: "BAANOOL limitó temporalmente las solicitudes. Espera antes de volver a intentar.",
        ERROR_RED: "No se pudo comunicar con BAANOOL (error de red).",
        TIMEOUT: "BAANOOL no respondió a tiempo.",
    }
    base = textos.get(codigo)
    if base:
        return base
    return f"No se pudo completar la adquisición ({codigo}): {detalle}" if detalle else \
        f"No se pudo completar la adquisición ({codigo})."


def _resumen_adquisicion(mapeo, lote) -> dict[str, int]:
    m = mapeo.resumen()
    return {
        "dispositivos_baanool": m["dispositivos"],
        "coincidencias_exactas": m["exactas"],
        "con_gpx": lote.cuenta(DESCARGADO) + lote.cuenta(YA_EXISTE),
        "descargados_ahora": lote.cuenta(DESCARGADO),
        "ya_existentes": lote.cuenta(YA_EXISTE),
        "sin_datos_periodo": lote.cuenta(SIN_DATOS_PERIODO),
        "solo_local": m["solo_local"],
        "sin_coordenadas": lote.cuenta(SIN_COORDENADAS),
        "errores": len(lote.errores),
        "no_procesadas": len(lote.pendientes),
        "posibles_coincidencias": m["posibles"],
        "conflictos": m["conflictos"],
    }


def actualizar_desde_baanool(inicio: datetime, fin: datetime, carpeta_entrada: Path, archivo_excel: Path,
                             raiz: Path, carpeta_manifiestos: Path | None = None,
                             on_progreso: Callable[[int, int, str, str], None] | None = None,
                             sesion=None, token: str | None = None, pausar=None) -> ResultadoAdquisicion:
    """Consulta dispositivos, hace el mapeo, descarga secuencialmente (respetando YA_EXISTE) y actualiza
    el manifiesto. NO analiza. Los errores de BAANOOL se devuelven en el resultado (no se lanzan): la
    interfaz y sus resultados previos siguen intactos.

    on_progreso(actual, total, llave, estado) recibe el avance; nunca recibe DNO ni token.
    pausar: solo para pruebas; por defecto se usa la pausa real entre vehículos.
    """
    clave = periodo_key(inicio, fin)

    def con_error(codigo: str, mensaje: str) -> ResultadoAdquisicion:
        return ResultadoAdquisicion(clave, False, codigo, mensaje, {}, None, False, mensaje)

    if fin <= inicio:
        return con_error("PARAMETROS_INVALIDOS", "El inicio del periodo debe ser anterior al fin")
    try:
        token = token if token is not None else obtener_token()
        sesion = sesion or crear_sesion()
        mapeo, _advertencias = preparar_mapeo(token, archivo_excel, sesion)
    except ErrorBaanool as exc:
        return con_error(exc.codigo, mensaje_para_usuario(exc.codigo, exc.mensaje))
    except Exception as exc:  # p. ej. no se pudo leer el Excel
        return con_error("ERROR_CATALOGO", f"No se pudo leer el catálogo de LLAVEs ({type(exc).__name__})")

    def progreso(indice: int, total: int, resultado) -> None:
        if on_progreso:
            on_progreso(indice, total, resultado.llave, resultado.estado)

    opciones = {} if pausar is None else {"pausar": pausar}
    lote = ejecutar_lote(mapeo, inicio, fin, carpeta_entrada, token=token, sesion=sesion, progreso=progreso,
                         **opciones)

    ruta = nota = None
    try:
        escrito = actualizar_manifiesto(lote, mapeo, raiz, carpeta_manifiestos)
        try:
            ruta = escrito.resolve().relative_to(raiz.resolve()).as_posix()
        except ValueError:
            ruta = f"{escrito.parent.name}/{escrito.name}"
    except ErrorEscrituraManifiesto as exc:
        nota = f"No se pudo actualizar el manifiesto: {exc}. Los GPX descargados no se afectan."

    codigo = mensaje = None
    if lote.estado == LOTE_DETENIDO_RATE_LIMIT:
        codigo = RATE_LIMIT_BAANOOL
    elif lote.estado == LOTE_DETENIDO_TOKEN:
        codigo = TOKEN_AUSENTE if TOKEN_AUSENTE in (lote.motivo_detencion or "") else TOKEN_INVALIDO
    if codigo:
        mensaje = mensaje_para_usuario(codigo)

    return ResultadoAdquisicion(
        clave_periodo=clave, ok=lote.estado == LOTE_COMPLETADO and nota is None, codigo_error=codigo,
        mensaje=mensaje, resumen=_resumen_adquisicion(mapeo, lote), ruta_manifiesto=ruta,
        lote_completo=lote.estado == LOTE_COMPLETADO and lote.seleccionadas == lote.exactas_totales,
        motivo_interrupcion=lote.motivo_detencion, nota_manifiesto=nota)


# ---------- estado de sesión (puro: sirve con st.session_state o con un dict) ----------

CLAVES_ADQUISICION = ("adquisicion",)
CLAVES_ANALISIS = ("evaluaciones", "resumen", "csv", "info", "periodo_analizado", "manifiesto_usado")


def aplicar_resultado_adquisicion(estado: MutableMapping, resultado: ResultadoAdquisicion) -> None:
    """Guarda SOLO lo no sensible de la adquisición. No toca los resultados de análisis."""
    estado["adquisicion"] = {
        "clave_periodo": resultado.clave_periodo, "ok": resultado.ok, "codigo_error": resultado.codigo_error,
        "mensaje": resultado.mensaje, "resumen": dict(resultado.resumen),
        "ruta_manifiesto": resultado.ruta_manifiesto, "lote_completo": resultado.lote_completo,
        "motivo_interrupcion": resultado.motivo_interrupcion, "nota_manifiesto": resultado.nota_manifiesto,
    }


def aplicar_resultado_analisis(estado: MutableMapping, resultado: ResultadoAnalisisPeriodo,
                               resumen, csv: str, hora: datetime, modo: str = "PERIODO") -> None:
    estado["evaluaciones"] = resultado.evaluaciones
    estado["resumen"] = resumen
    estado["csv"] = csv
    estado["info"] = {"modo": modo, "hora": hora, "entrada": "manifiesto", "aviso_excel": None,
                      "geofencing": True}
    estado["periodo_analizado"] = {"clave": resultado.clave_periodo,
                                   "inicio": resultado.inicio.strftime("%Y-%m-%d %H:%M:%S"),
                                   "fin": resultado.fin.strftime("%Y-%m-%d %H:%M:%S"), "modo": modo}
    estado["manifiesto_usado"] = {"ruta": resultado.ruta_manifiesto,
                                  "fecha_ejecucion": resultado.fecha_ejecucion_manifiesto,
                                  "lote_completo": resultado.lote_completo,
                                  "motivo_interrupcion": resultado.motivo_interrupcion}
    estado["mapas"] = {}
    estado["mapa_abierto"] = None


def advertencia_periodo(clave_seleccionada: str, estado: MutableMapping) -> str | None:
    """Texto de advertencia si el periodo seleccionado difiere del analizado (o None)."""
    analizado = estado.get("periodo_analizado")
    if not analizado or analizado.get("modo") != "PERIODO":
        return None
    if analizado["clave"] == clave_seleccionada:
        return None
    return (f"Los resultados mostrados corresponden al periodo {analizado['inicio']} -> {analizado['fin']}, "
            "que es distinto del periodo seleccionado ahora. Pulsa «Analizar periodo» para actualizarlos.")


def puede_generar_mapa(evaluacion: EvaluacionVehiculo) -> bool:
    """Solo hay mapa con un análisis válido y posiciones: nunca para SIN_DATOS_PERIODO,
    SIN_DISPOSITIVO_BAANOOL, ERROR_ADQUISICION ni SIN_GPX."""
    return evaluacion.tiene_datos and bool(evaluacion.resultado.puntos)
