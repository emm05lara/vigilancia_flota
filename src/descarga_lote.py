"""Descarga por lote (etapa 6B-2): un GPX por cada coincidencia EXACTA LLAVE <-> dispositivo.

Reutiliza sin cambios lo ya validado:
- src/baanool.py: token, exportación (con conversión CDMX -> BAANOOL, Referer y User-Agent),
  descarga y validación del GPX.
- src/mapeo_baanool.py: el mapeo; SOLO `mapeo.exactas` puede generar una petición.
- crear_carpetas_llaves.limpiar_nombre_carpeta: carpeta local de la LLAVE.

SIN_DATOS_PERIODO describe solo lo observado: BAANOOL respondió para ese dispositivo y periodo pero no
entregó posiciones GPS. NO es un error y NO se interpreta como avería, chip desactivado, pérdida de
señal ni vehículo apagado: el sistema no tiene evidencia de la causa. En ese caso no se guarda ningún
archivo. El estado actual ONLINE/OFFLINE (fotografía de getMyDeviceListData) se conserva aparte y no
describe lo ocurrido en el periodo.

Estrictamente SECUENCIAL, con pausa entre vehículos. Se detiene de inmediato ante token ausente,
token inválido o rate limit (sin reintentos). Cualquier otro error afecta solo a ese vehículo.
Nunca borra ni sobrescribe archivos; un archivo existente con el nombre del periodo es YA_EXISTE y
no se vuelve a pedir. No analiza la flotilla al terminar.

El token no se guarda, no se imprime y no aparece en los resultados.
"""

import math
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from crear_carpetas_llaves import limpiar_nombre_carpeta

from .baanool import (RATE_LIMIT_BAANOOL, TOKEN_AUSENTE, TOKEN_INVALIDO, USER_AGENT_BAANOOL, ErrorBaanool,
                      crear_sesion, descargar_gpx, obtener_dispositivos, obtener_token,
                      solicitar_exportacion_gpx, validar_gpx_descargado)
from .llaves import cargar_llaves, clave_carpeta
from .mapeo_baanool import ResultadoMapeoBaanool, mapear_llaves_con_dispositivos
from .parametros import PAUSA_ENTRE_DESCARGAS_SEGUNDOS

# Estado de cada vehículo
DESCARGADO = "DESCARGADO"
YA_EXISTE = "YA_EXISTE"
SIN_COORDENADAS = "SIN_COORDENADAS"
ERROR_EXPORTACION = "ERROR_EXPORTACION"
ERROR_DESCARGA = "ERROR_DESCARGA"
GPX_INVALIDO = "GPX_INVALIDO"  # el archivo no es un GPX legible (no es "sin posiciones")
SIN_DATOS_PERIODO = "SIN_DATOS_PERIODO"  # respuesta válida sin posiciones GPS: NO es un error
CARPETA_AMBIGUA = "CARPETA_AMBIGUA"  # dos LLAVEs exactas comparten carpeta: no se descarga ninguna
POR_DESCARGAR = "POR_DESCARGAR"  # solo en --dry-run

# Estado general del lote
LOTE_COMPLETADO = "COMPLETADO"
LOTE_DRY_RUN = "DRY_RUN"
LOTE_DETENIDO_RATE_LIMIT = "LOTE_DETENIDO_RATE_LIMIT"
LOTE_DETENIDO_TOKEN = "LOTE_DETENIDO_TOKEN"

CODIGOS_QUE_DETIENEN_EL_LOTE = (TOKEN_AUSENTE, TOKEN_INVALIDO, RATE_LIMIT_BAANOOL)
CODIGOS_GPX_INVALIDO = ("GPX_INVALIDO", "GPX_SIN_PUNTOS")


@dataclass(frozen=True)
class ResultadoVehiculoLote:
    llave: str
    estado: str
    mensaje: str = ""
    ruta_archivo: Path | None = None
    cantidad_puntos: int | None = None
    # Fotografía ACTUAL de getMyDeviceListData (ONLINE/OFFLINE). NO describe el periodo solicitado.
    estado_actual_baanool: str | None = None


@dataclass
class ResultadoLote:
    estado: str
    inicio: datetime
    fin: datetime
    resumen_mapeo: dict[str, int]
    exactas_totales: int  # coincidencias exactas disponibles
    seleccionadas: int  # las que entraron al lote (tras --limite)
    vehiculos: list[ResultadoVehiculoLote] = field(default_factory=list)
    pendientes: list[str] = field(default_factory=list)  # seleccionadas que no se procesaron
    detenido_en: str | None = None
    motivo_detencion: str | None = None
    segundos: float = 0.0

    def cuenta(self, estado: str) -> int:
        return sum(1 for v in self.vehiculos if v.estado == estado)

    @property
    def errores(self) -> list[ResultadoVehiculoLote]:
        return [v for v in self.vehiculos
                if v.estado in (ERROR_EXPORTACION, ERROR_DESCARGA, GPX_INVALIDO, CARPETA_AMBIGUA)]

    @property
    def procesados(self) -> int:
        return len(self.vehiculos)


# ---------- piezas ----------

def nombre_archivo_lote(inicio: datetime, fin: datetime) -> str:
    """baanool_2026-10-10_000000_2026-10-10_235959.gpx (periodo LOCAL solicitado; sin DNO)."""
    return f"baanool_{inicio:%Y-%m-%d_%H%M%S}_{fin:%Y-%m-%d_%H%M%S}.gpx"


def coordenadas_validas(lat: float | None, lng: float | None) -> bool:
    """Coordenadas utilizables para el Referer. (0, 0) se trata como ausente: es un valor por defecto."""
    if lat is None or lng is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False
    return not (lat == 0 and lng == 0)


def preparar_mapeo(token: str, archivo_excel: Path, sesion=None) -> tuple[ResultadoMapeoBaanool, int]:
    """Consulta los dispositivos, lee las LLAVEs y construye el mapeo existente. Devuelve (mapeo, n_advertencias)."""
    consulta = obtener_dispositivos(token, sesion)
    mapeo = mapear_llaves_con_dispositivos(cargar_llaves(archivo_excel), consulta.dispositivos)
    return mapeo, len(consulta.advertencias)


def _estado_actual(online: bool | None) -> str | None:
    return None if online is None else ("ONLINE" if online else "OFFLINE")


def _sin_token(texto: str, token: str | None) -> str:
    return texto.replace(token, "***") if token else texto


def _procesar_vehiculo(llave, dispositivo, inicio, fin, carpeta_entrada: Path, token, sesion, dry_run
                       ) -> tuple[ResultadoVehiculoLote, bool]:
    """Devuelve (resultado, hizo_peticiones). Relanza ErrorBaanool solo si debe detener el lote."""
    carpeta = carpeta_entrada / limpiar_nombre_carpeta(llave)
    destino = carpeta / nombre_archivo_lote(inicio, fin)

    if destino.exists():  # no se pide de nuevo ni se sobrescribe
        return ResultadoVehiculoLote(llave, YA_EXISTE, "El archivo del periodo ya existe", destino), False
    if not coordenadas_validas(dispositivo.lat, dispositivo.lng):
        return ResultadoVehiculoLote(llave, SIN_COORDENADAS,
                                     "El dispositivo no tiene coordenadas válidas; no se intentó descargar"), False
    if dry_run:
        return ResultadoVehiculoLote(llave, POR_DESCARGAR, "Se descargaría", destino), False

    def falla(estado: str, exc: ErrorBaanool | Exception, hizo: bool = True):
        if isinstance(exc, ErrorBaanool):
            if exc.codigo in CODIGOS_QUE_DETIENEN_EL_LOTE:
                raise exc
            texto = f"[{exc.codigo}] {exc.mensaje}"
        else:
            texto = f"{type(exc).__name__}"
        return ResultadoVehiculoLote(llave, estado, _sin_token(texto, token)), hizo

    try:
        carpeta.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return falla(ERROR_DESCARGA, exc, hizo=False)

    try:
        exportacion = solicitar_exportacion_gpx(dispositivo.dno, inicio, fin, token, sesion,
                                                latitud=dispositivo.lat, longitud=dispositivo.lng,
                                                user_agent=USER_AGENT_BAANOOL)
    except ErrorBaanool as exc:
        return falla(ERROR_EXPORTACION, exc)

    # Se descarga a un archivo temporal dentro de la carpeta del vehículo (extensión que el análisis
    # ignora) y solo un GPX con posiciones se mueve al nombre final. Así una respuesta sin posiciones
    # no deja ningún archivo y nunca se sobrescribe nada.
    temporal = carpeta / (destino.name + ".descarga")
    marcado = temporal.with_name(temporal.name + ".invalido")  # donde validar_gpx_descargado lo deja si falla
    try:
        descargar_gpx(exportacion.url_descarga, temporal, token, sesion)
    except ErrorBaanool as exc:
        return falla(ERROR_DESCARGA, exc)
    except Exception as exc:  # p. ej. un fallo inesperado de disco
        return falla(ERROR_DESCARGA, exc)

    try:
        validos, _descartados, _puntos = validar_gpx_descargado(temporal)
    except ErrorBaanool as exc:
        if exc.codigo == "GPX_SIN_PUNTOS":  # BAANOOL respondió, sin posiciones: observación, no error
            marcado.unlink(missing_ok=True)
            return ResultadoVehiculoLote(
                llave, SIN_DATOS_PERIODO,
                "BAANOOL respondió para este dispositivo y periodo, pero no entregó posiciones GPS"), True
        resultado, hizo = falla(GPX_INVALIDO, exc)  # archivo ilegible: se conserva para diagnóstico
        conservado = destino.with_name(destino.name + ".invalido")
        try:
            if marcado.exists():
                marcado.replace(conservado)
        except OSError:
            conservado = marcado
        return ResultadoVehiculoLote(llave, GPX_INVALIDO, resultado.mensaje,
                                     conservado if conservado.exists() else None), hizo

    try:
        if destino.exists():  # carrera improbable: nunca se sobrescribe
            temporal.unlink(missing_ok=True)
            return ResultadoVehiculoLote(llave, YA_EXISTE, "El archivo del periodo ya existe", destino), True
        temporal.rename(destino)
    except OSError as exc:
        return falla(ERROR_DESCARGA, exc)

    return ResultadoVehiculoLote(llave, DESCARGADO, "GPX validado", destino, validos), True


def ejecutar_lote(mapeo: ResultadoMapeoBaanool, inicio: datetime, fin: datetime, carpeta_entrada: Path,
                  token: str | None = None, sesion=None, dry_run: bool = False, limite: int | None = None,
                  pausar=time.sleep, pausa_segundos: float = PAUSA_ENTRE_DESCARGAS_SEGUNDOS,
                  progreso=lambda indice, total, resultado: None) -> ResultadoLote:
    """Procesa SOLO `mapeo.exactas`, de forma secuencial y en orden determinista por LLAVE.

    inicio y fin son hora local (CDMX). `limite` selecciona como máximo N exactas (por orden de LLAVE).
    """
    t0 = time.monotonic()
    exactas = mapeo.exactas
    ordenadas = sorted(exactas)
    if limite is not None:
        ordenadas = ordenadas[:max(limite, 0)]

    lote = ResultadoLote(
        estado=LOTE_DRY_RUN if dry_run else LOTE_COMPLETADO, inicio=inicio, fin=fin,
        resumen_mapeo=mapeo.resumen(), exactas_totales=len(exactas), seleccionadas=len(ordenadas))

    def detener(estado: str, motivo: str, desde: int, llave: str | None = None) -> ResultadoLote:
        lote.estado, lote.motivo_detencion, lote.detenido_en = estado, motivo, llave
        lote.pendientes = ordenadas[desde:]
        lote.segundos = time.monotonic() - t0
        return lote

    if not dry_run:
        try:
            token = token if token is not None else obtener_token()
        except ErrorBaanool as exc:
            return detener(LOTE_DETENIDO_TOKEN, f"[{exc.codigo}] {exc.mensaje}", 0)
        sesion = sesion or crear_sesion()

    # Dos LLAVEs exactas con la misma carpeta mezclarían GPX de vehículos distintos: no se descargan.
    por_carpeta: dict[str, list[str]] = {}
    for llave in ordenadas:
        por_carpeta.setdefault(clave_carpeta(limpiar_nombre_carpeta(llave)), []).append(llave)
    ambiguas = {llave for grupo in por_carpeta.values() if len(grupo) > 1 for llave in grupo}

    total = len(ordenadas)
    for indice, llave in enumerate(ordenadas):
        if llave in ambiguas:
            resultado, hizo = ResultadoVehiculoLote(
                llave, CARPETA_AMBIGUA, "Otra LLAVE exacta usa la misma carpeta local; no se descarga"), False
        else:
            try:
                resultado, hizo = _procesar_vehiculo(llave, exactas[llave], inicio, fin, carpeta_entrada,
                                                     token, sesion, dry_run)
            except ErrorBaanool as exc:
                motivo = _sin_token(f"[{exc.codigo}] {exc.mensaje}", token)
                lote.vehiculos.append(ResultadoVehiculoLote(
                    llave, ERROR_EXPORTACION, motivo,
                    estado_actual_baanool=_estado_actual(exactas[llave].online)))
                progreso(indice + 1, total, lote.vehiculos[-1])
                estado = LOTE_DETENIDO_RATE_LIMIT if exc.codigo == RATE_LIMIT_BAANOOL else LOTE_DETENIDO_TOKEN
                return detener(estado, motivo, indice + 1, llave)
        resultado = replace(resultado, estado_actual_baanool=_estado_actual(exactas[llave].online))
        lote.vehiculos.append(resultado)
        progreso(indice + 1, total, resultado)
        if hizo and indice < total - 1:  # pausa solo si hubo peticiones y queda otro vehículo
            pausar(pausa_segundos)

    lote.segundos = time.monotonic() - t0
    return lote
