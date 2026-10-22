"""Comunicación HTTP con BAANOOL (prototipo de la etapa 6A: UN GPX a la vez).

Flujo observado al exportar manualmente una trayectoria:
  1. GET .../exportAckWarmInfoDataList?dno=..&startTime=..&endTime=..&exportType=2
     con el header "token". Responde JSON {"status": 0, "resmsg": "SUCCESS", "data": "<url>"}.
  2. GET <url> -> el archivo GPX.

Todo el HTTP con BAANOOL vive aquí; el motor de análisis no sabe nada de BAANOOL.

SEGURIDAD: el token es una credencial temporal de sesión. Se lee SOLO de la variable de entorno
BAANOOL_TOKEN, nunca se escribe en disco, ni se imprime, ni aparece en mensajes de error.
Además, el token solo se envía a hosts de BAANOOL (si la URL de descarga apuntara a otro
dominio, la descarga se rechaza en lugar de enviarle la credencial).

Headers: solo los mínimos ("token" y "Accept"), más "Referer" en la solicitud de exportación
cuando se conocen lat/lng (la petición manual exitosa lo incluye; con Referer solo, BAANOOL
siguió respondiendo status=105) y, de forma opcional y explícita, "User-Agent" de navegador
(USER_AGENT_BAANOOL) en esa misma solicitud. Por defecto no se envía User-Agent propio
(requests agrega el suyo). Nunca se envían cookies (las sesiones aquí las rechazan), ni Origin,
ni Sec-*, ni Accept-Language. Cualquier otro header que resulte necesario debe documentarse
aquí con su motivo. Se cambia UNA variable por prueba.
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests

from .gpx_parser import leer_gpx
from .parametros import TIMEOUT_BAANOOL_SEGUNDOS, ZONA_HORARIA_BAANOOL, ZONA_HORARIA_LOCAL

URL_EXPORTACION = ("https://www.baanooliot.com/car-api/bnclw-onboard/devReportController/"
                   "exportAckWarmInfoDataList")
DOMINIO_BAANOOL = "baanooliot.com"  # la URL de descarga debe ser de este dominio (o subdominio)
URL_REFERER = "https://www.baanooliot.com/playGoogle"  # página desde la que el navegador exporta
# User-Agent de la petición manual exitosa (Chrome). No es un dato sensible.
USER_AGENT_BAANOOL = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/154.0.0.0 Safari/537.36")
VARIABLE_TOKEN = "BAANOOL_TOKEN"
FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"
TIPO_EXPORTACION_GPX = "2"
STATUS_EXITO = 0
STATUS_SIN_PERMISO = 105  # resmsg observado: "\u6682\u65e0\u6743\u9650"
STATUS_DEMASIADAS_SOLICITUDES = 5550  # resmsg observado: "Too many requests"


def convertir_fecha_local_a_baanool(fecha: datetime) -> datetime:
    """Convierte una fecha en hora local (America/Mexico_City) a la que espera el endpoint de BAANOOL.

    Hallazgo experimental: startTime/endTime se interpretan en la hora del backend (Asia/Shanghai,
    UTC+8). La conversión usa zoneinfo (no se suma un desfase fijo), así que respeta el horario de
    verano que México tuvo hasta 2022.

    - fecha sin zona horaria: se interpreta como hora local de Ciudad de México.
    - fecha con zona horaria: se convierte correctamente desde su propia zona.
    Devuelve la fecha SIN información de zona, lista para formatearse con FORMATO_FECHA.

    Aplica solo a startTime/endTime de la petición: los timestamps del GPX no se convierten.
    """
    local = ZoneInfo(ZONA_HORARIA_LOCAL)
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=local)
    return fecha.astimezone(ZoneInfo(ZONA_HORARIA_BAANOOL)).replace(tzinfo=None)


class ErrorBaanool(Exception):
    """Error del flujo de descarga. `codigo` identifica la categoría; el mensaje nunca incluye el token."""

    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


# Códigos de error
TOKEN_AUSENTE = "TOKEN_AUSENTE"
TOKEN_INVALIDO = "TOKEN_INVALIDO"
TIMEOUT = "TIMEOUT"
ERROR_RED = "ERROR_RED"
ERROR_HTTP = "ERROR_HTTP"
JSON_INVALIDO = "JSON_INVALIDO"
STATUS_BAANOOL = "STATUS_BAANOOL"
PERMISO_BAANOOL = "PERMISO_BAANOOL"
RATE_LIMIT_BAANOOL = "RATE_LIMIT_BAANOOL"
DATA_AUSENTE = "DATA_AUSENTE"
URL_INVALIDA = "URL_INVALIDA"
DESCARGA_NO_GPX = "DESCARGA_NO_GPX"
GPX_INVALIDO = "GPX_INVALIDO"
GPX_SIN_PUNTOS = "GPX_SIN_PUNTOS"
ERROR_ARCHIVO = "ERROR_ARCHIVO"
PARAMETROS_INVALIDOS = "PARAMETROS_INVALIDOS"


@dataclass(frozen=True)
class RespuestaExportacion:
    http_status: int
    status: int
    resmsg: str
    url_descarga: str
    inicio_enviado: str = ""  # startTime tal como se envió a BAANOOL (ya convertido)
    fin_enviado: str = ""  # endTime tal como se envió a BAANOOL (ya convertido)


@dataclass(frozen=True)
class ResultadoDescarga:
    ruta: Path
    tamano_bytes: int
    puntos_validos: int
    puntos_descartados: int
    primer_timestamp: datetime
    ultimo_timestamp: datetime
    coordenada_inicial: tuple[float, float]
    coordenada_final: tuple[float, float]
    exportacion: RespuestaExportacion


# ---------- token ----------

def obtener_token() -> str:
    """Lee el token de la variable de entorno. No lo valida ni lo adivina."""
    token = os.environ.get(VARIABLE_TOKEN, "").strip()
    if not token:
        raise ErrorBaanool(
            TOKEN_AUSENTE,
            f"No hay token: define la variable de entorno {VARIABLE_TOKEN} con el token de la sesión "
            f"actual de BAANOOL (en PowerShell: $env:{VARIABLE_TOKEN}=\"<token>\"). No se hizo ninguna petición.")
    return token


def _sin_token(texto: str, token: str) -> str:
    """Defensa en profundidad: ningún texto que salga del módulo debe contener el token."""
    return texto.replace(token, "***") if token else texto


def _encabezados(token: str, referer: str | None = None, user_agent: str | None = None) -> dict[str, str]:
    encabezados = {"token": token, "Accept": "application/json, text/plain, */*"}
    if referer:
        encabezados["Referer"] = referer
    if user_agent:
        encabezados["User-Agent"] = user_agent
    return encabezados


def construir_referer(dno: str, latitud: float, longitud: float) -> str:
    """https://www.baanooliot.com/playGoogle?dno=<DNO>&lng=<LNG>&lat=<LAT> (query escapada por urlencode)."""
    for nombre, valor, limite in (("latitud", latitud, 90), ("longitud", longitud, 180)):
        if not isinstance(valor, (int, float)) or isinstance(valor, bool) or not -limite <= valor <= limite:
            raise ErrorBaanool(PARAMETROS_INVALIDOS, f"{nombre} inválida: debe ser un número entre "
                                                     f"-{limite} y {limite}.")
    if not str(dno).strip():
        raise ErrorBaanool(PARAMETROS_INVALIDOS, "El DNO no puede estar vacío.")
    return URL_REFERER + "?" + urlencode({"dno": str(dno).strip(), "lng": longitud, "lat": latitud})


def crear_sesion() -> requests.Session:
    """Sesión HTTP que NO guarda ni reenvía cookies (ninguna se envía a BAANOOL en este prototipo)."""
    sesion = requests.Session()
    sesion.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # rechaza todas las cookies
    return sesion


# ---------- 1. solicitud de exportación ----------

def solicitar_exportacion_gpx(dno: str, fecha_inicio: datetime, fecha_fin: datetime, token: str,
                              sesion=None, latitud: float | None = None,
                              longitud: float | None = None,
                              user_agent: str | None = None) -> RespuestaExportacion:
    """Pide a BAANOOL que genere el GPX y devuelve la URL de descarga que indica.

    fecha_inicio y fecha_fin son hora LOCAL (Ciudad de México): aquí se convierten a la hora que
    espera BAANOOL (convertir_fecha_local_a_baanool) antes de enviarlas como startTime/endTime.

    Si se dan latitud y longitud, la petición lleva el header Referer de la página playGoogle.
    Si se da user_agent (p. ej. USER_AGENT_BAANOOL), se envía como header User-Agent.
    """
    if not str(dno).strip():
        raise ErrorBaanool(PARAMETROS_INVALIDOS, "El DNO no puede estar vacío.")
    if fecha_fin <= fecha_inicio:
        raise ErrorBaanool(PARAMETROS_INVALIDOS, "La fecha de fin debe ser posterior a la de inicio.")

    if (latitud is None) != (longitud is None):
        raise ErrorBaanool(PARAMETROS_INVALIDOS, "Para enviar el Referer se necesitan latitud y longitud, "
                                                 "o ninguna de las dos.")
    referer = construir_referer(dno, latitud, longitud) if latitud is not None else None

    sesion = sesion or crear_sesion()
    parametros = {  # requests construye y codifica la query; no se concatena a mano
        "dno": str(dno).strip(),
        "startTime": convertir_fecha_local_a_baanool(fecha_inicio).strftime(FORMATO_FECHA),
        "endTime": convertir_fecha_local_a_baanool(fecha_fin).strftime(FORMATO_FECHA),
        "exportType": TIPO_EXPORTACION_GPX,
    }
    try:
        respuesta = sesion.get(URL_EXPORTACION, params=parametros, headers=_encabezados(token, referer, user_agent),
                               timeout=TIMEOUT_BAANOOL_SEGUNDOS)
    except requests.exceptions.Timeout:
        raise ErrorBaanool(TIMEOUT, f"BAANOOL no respondió en {TIMEOUT_BAANOOL_SEGUNDOS} s "
                                    "(solicitud de exportación).") from None
    except requests.exceptions.RequestException as exc:
        raise ErrorBaanool(ERROR_RED, "Error de red al solicitar la exportación: "
                                      f"{_sin_token(type(exc).__name__, token)}.") from None

    if respuesta.status_code in (401, 403):
        raise ErrorBaanool(TOKEN_INVALIDO, f"BAANOOL rechazó el token (HTTP {respuesta.status_code}): "
                                           "la sesión pudo vencer. Proporciona un token nuevo.")
    if respuesta.status_code != 200:
        raise ErrorBaanool(ERROR_HTTP, f"BAANOOL respondió HTTP {respuesta.status_code} "
                                       "a la solicitud de exportación.")

    try:
        cuerpo = respuesta.json()
    except ValueError:
        raise ErrorBaanool(JSON_INVALIDO, "La respuesta de la exportación no es un JSON válido "
                                          "(HTTP 200).") from None
    if not isinstance(cuerpo, dict):
        raise ErrorBaanool(JSON_INVALIDO, "La respuesta de la exportación no tiene la estructura esperada.")

    status, resmsg = cuerpo.get("status"), _sin_token(str(cuerpo.get("resmsg")), token)
    if status == STATUS_DEMASIADAS_SOLICITUDES:
        raise ErrorBaanool(RATE_LIMIT_BAANOOL, "BAANOOL limitó temporalmente las solicitudes. "
                                               "Espera antes de volver a intentar.")
    if status == STATUS_SIN_PERMISO:  # NO se asume que signifique sesión vencida
        raise ErrorBaanool(PERMISO_BAANOOL, f"BAANOOL respondió status={status}, resmsg={resmsg!r}: la "
                                            "solicitud no tiene permiso para esta exportación. No hay "
                                            "evidencia de que esto signifique sesión vencida.")
    if status != STATUS_EXITO:
        pista = " La sesión pudo vencer: proporciona un token nuevo." if status in (401, 403) else ""
        raise ErrorBaanool(STATUS_BAANOOL, f"BAANOOL respondió HTTP 200 pero status={status!r}, "
                                           f"resmsg={resmsg!r}.{pista}")

    url = cuerpo.get("data")
    if url is None or (isinstance(url, str) and not url.strip()):
        raise ErrorBaanool(DATA_AUSENTE, f"BAANOOL indicó éxito (status={status}, resmsg={resmsg!r}) "
                                         "pero no devolvió 'data' (URL de descarga).")
    if not isinstance(url, str):
        raise ErrorBaanool(URL_INVALIDA, "El campo 'data' no es una URL de texto.")
    _validar_url_descarga(url)
    return RespuestaExportacion(respuesta.status_code, status, resmsg, url.strip(),
                                parametros["startTime"], parametros["endTime"])


def _validar_url_descarga(url: str) -> None:
    partes = urlparse(url.strip())
    host = (partes.hostname or "").lower()
    if partes.scheme != "https" or not host:  # solo HTTPS: el token no debe viajar en claro
        raise ErrorBaanool(URL_INVALIDA, "La URL de descarga que devolvió BAANOOL no es válida.")
    if host != DOMINIO_BAANOOL and not host.endswith("." + DOMINIO_BAANOOL):
        # No se le envía el token a un dominio ajeno.
        raise ErrorBaanool(URL_INVALIDA, f"La URL de descarga apunta a un dominio ajeno a BAANOOL ({host}); "
                                         "no se descarga para no enviarle la credencial.")


# ---------- 2. descarga ----------

def _parece_gpx(contenido: bytes) -> bool:
    inicio = contenido[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return inicio.startswith(b"<?xml") or inicio.startswith(b"<gpx")


def descargar_gpx(url: str, destino: Path, token: str, sesion=None) -> Path:
    """Descarga el archivo de `url` a `destino` (conservador: se envía el mismo header de token).

    Verifica que el contenido sea claramente un GPX/XML, no un JSON o HTML de error; la URL se
    usa exactamente como la devolvió BAANOOL (incluido el parámetro `bucktName`).
    """
    _validar_url_descarga(url)
    sesion = sesion or crear_sesion()
    try:
        respuesta = sesion.get(url, headers=_encabezados(token), timeout=TIMEOUT_BAANOOL_SEGUNDOS)
    except requests.exceptions.Timeout:
        raise ErrorBaanool(TIMEOUT, f"BAANOOL no respondió en {TIMEOUT_BAANOOL_SEGUNDOS} s "
                                    "(descarga del GPX).") from None
    except requests.exceptions.RequestException as exc:
        raise ErrorBaanool(ERROR_RED, f"Error de red al descargar el GPX: {type(exc).__name__}.") from None

    if respuesta.status_code in (401, 403):
        raise ErrorBaanool(TOKEN_INVALIDO, f"La descarga fue rechazada (HTTP {respuesta.status_code}): "
                                           "el token no basta o la sesión venció.")
    if respuesta.status_code != 200:
        raise ErrorBaanool(ERROR_HTTP, f"La descarga del GPX respondió HTTP {respuesta.status_code}.")

    contenido = respuesta.content
    if not contenido:
        raise ErrorBaanool(DESCARGA_NO_GPX, "La descarga devolvió un archivo vacío.")
    if not _parece_gpx(contenido):
        tipo = (respuesta.headers.get("Content-Type") or "desconocido").split(";")[0]
        raise ErrorBaanool(DESCARGA_NO_GPX, f"La descarga no es un GPX (Content-Type: {tipo}); "
                                            "parece una respuesta de error. No se guardó el archivo.")

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido)
    except OSError as exc:
        raise ErrorBaanool(ERROR_ARCHIVO, f"No se pudo guardar el archivo ({type(exc).__name__}).") from None
    return destino


# ---------- 3. validación con el parser existente ----------

def validar_gpx_descargado(ruta: Path) -> tuple[int, int, list]:
    """Lee el archivo con el parser del proyecto. Devuelve (válidos, descartados, puntos).

    Si no es válido, el archivo se conserva renombrado como `.invalido` para diagnóstico y se
    lanza ErrorBaanool; así nunca queda un .gpx que parezca una descarga correcta.
    """
    try:
        puntos, descartados = leer_gpx(ruta)
    except Exception as exc:
        conservado = _marcar_invalido(ruta)
        raise ErrorBaanool(GPX_INVALIDO, f"El archivo descargado no se pudo leer como GPX "
                                         f"({type(exc).__name__}). Conservado para diagnóstico en: "
                                         f"{conservado}") from None
    if not puntos:
        conservado = _marcar_invalido(ruta)
        raise ErrorBaanool(GPX_SIN_PUNTOS, f"El GPX descargado no contiene puntos GPS válidos "
                                           f"(descartados: {descartados}). Conservado para diagnóstico en: "
                                           f"{conservado}")
    return len(puntos), descartados, puntos


def _marcar_invalido(ruta: Path) -> Path:
    destino = ruta.with_name(ruta.name + ".invalido")
    try:
        ruta.replace(destino)
    except OSError:
        return ruta
    return destino


# ---------- nombre local ----------

def _seguro(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(texto).strip()) or "sin_dno"


def nombre_archivo_local(dno: str, fecha_inicio: datetime, fecha_fin: datetime) -> str:
    """<DNO>_2026-10-12_100008_105008.gpx (con la fecha final si cruza de día)."""
    partes = [_seguro(dno), f"{fecha_inicio:%Y-%m-%d}", f"{fecha_inicio:%H%M%S}"]
    if fecha_fin.date() != fecha_inicio.date():
        partes.append(f"{fecha_fin:%Y-%m-%d}")
    partes.append(f"{fecha_fin:%H%M%S}")
    return "_".join(partes) + ".gpx"


# ---------- flujo completo ----------

def descargar_gpx_de_baanool(dno: str, fecha_inicio: datetime, fecha_fin: datetime, carpeta_destino: Path,
                             token: str | None = None, sesion=None, avisar=lambda mensaje: None,
                             latitud: float | None = None, longitud: float | None = None,
                             user_agent: str | None = None) -> ResultadoDescarga:
    """Exporta, descarga y valida UN GPX. `avisar` recibe mensajes de progreso (nunca el token).

    latitud/longitud (opcionales) se usan solo para el header Referer de la solicitud de exportación;
    user_agent (opcional) también va solo en esa solicitud.
    """
    token = token if token is not None else obtener_token()
    sesion = sesion or crear_sesion()
    try:
        avisar("Solicitando exportación...")
        exportacion = solicitar_exportacion_gpx(dno, fecha_inicio, fecha_fin, token, sesion, latitud, longitud,
                                                user_agent)
        avisar("Exportación preparada.")

        avisar("Descargando GPX...")
        destino = carpeta_destino / nombre_archivo_local(dno, fecha_inicio, fecha_fin)
        ruta = descargar_gpx(exportacion.url_descarga, destino, token, sesion)
        avisar("Archivo guardado.")

        validos, descartados, puntos = validar_gpx_descargado(ruta)
        avisar(f"Puntos válidos: {validos}")
        avisar("Descarga validada.")
    except ErrorBaanool as exc:  # garantía final: el mensaje que sale nunca contiene el token
        raise ErrorBaanool(exc.codigo, _sin_token(exc.mensaje, token)) from None

    primero, ultimo = puntos[0], puntos[-1]
    return ResultadoDescarga(
        ruta=ruta, tamano_bytes=ruta.stat().st_size, puntos_validos=validos, puntos_descartados=descartados,
        primer_timestamp=primero.fecha, ultimo_timestamp=ultimo.fecha,
        coordenada_inicial=(primero.latitud, primero.longitud),
        coordenada_final=(ultimo.latitud, ultimo.longitud), exportacion=exportacion)


# =====================================================================================
# Etapa 6B-1: descubrimiento de dispositivos (getMyDeviceListData)
# =====================================================================================

URL_DISPOSITIVOS = ("https://www.baanooliot.com/car-api/bnclw-onboard/personController/"
                    "getMyDeviceListData")
ORIGIN_BAANOOL = "https://www.baanooliot.com"
REFERER_RAIZ_BAANOOL = "https://www.baanooliot.com/"
CONTENT_TYPE_FORMULARIO = "application/x-www-form-urlencoded;charset=UTF-8"
PATRON_DNO_DISPOSITIVO = re.compile(r"^[A-Za-z0-9]+$")

DISPOSITIVOS_AUSENTES = "DISPOSITIVOS_AUSENTES"
DISPOSITIVOS_VACIOS = "DISPOSITIVOS_VACIOS"

# Problemas que puede traer un dispositivo mal formado (no detienen la lista completa).
NAME_VACIO = "NAME_VACIO"
NAME_INVALIDO = "NAME_INVALIDO"
DNO_VACIO = "DNO_VACIO"
DNO_INVALIDO = "DNO_INVALIDO"


@dataclass(frozen=True)
class DispositivoBaanool:
    """Solo los campos que usa el proyecto. `dno` es TEXTO (identificador técnico de BAANOOL)."""
    name: str  # BAANOOL lo usa con la forma de la LLAVE (p. ej. VENTO|2019|BLANCO|LUE965C); "" si falta
    dno: str | None  # None si falta o no es un identificador válido (ver `problemas`)
    car_num: str | None  # parece la placa; solo diagnóstico
    lat: float | None
    lng: float | None
    dev_status: object | None = None  # tal como lo entrega BAANOOL (diagnóstico)
    online: bool | None = None
    problemas: tuple[str, ...] = ()
    indice: int = -1  # posición en la respuesta (para referirse al dispositivo sin imprimir datos)

    @property
    def utilizable(self) -> bool:
        return not self.problemas


@dataclass(frozen=True)
class ResultadoDispositivos:
    http_status: int
    status: int
    resmsg: str
    dispositivos: list[DispositivoBaanool]
    advertencias: list[str]  # elementos que ni siquiera eran un objeto, etc.


def enmascarar_dno(dno: str | None) -> str:
    """Para diagnóstico: solo los últimos 4 caracteres."""
    if not dno:
        return "(vacío)"
    return "***" + dno[-4:] if len(dno) > 4 else "***"


def _encabezados_dispositivos(token: str) -> dict[str, str]:
    return {
        "token": token,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": CONTENT_TYPE_FORMULARIO,
        "Origin": ORIGIN_BAANOOL,
        "Referer": REFERER_RAIZ_BAANOOL,
        "User-Agent": USER_AGENT_BAANOOL,
    }


def _dno_a_texto(valor) -> tuple[str | None, str | None]:
    """(dno_texto, problema). El DNO siempre es texto; no se corrige nada."""
    if valor is None:
        return None, DNO_VACIO
    if isinstance(valor, bool) or isinstance(valor, float):
        return None, DNO_INVALIDO  # un identificador no debe llegar como decimal ni booleano
    if isinstance(valor, int):
        texto = str(valor)
    elif isinstance(valor, str):
        texto = valor.strip()
    else:
        return None, DNO_INVALIDO
    if not texto:
        return None, DNO_VACIO
    if not PATRON_DNO_DISPOSITIVO.match(texto):
        return None, DNO_INVALIDO
    return texto, None


def _numero(valor) -> float | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        try:
            return float(valor.strip())
        except ValueError:
            return None
    return None


def _booleano(valor) -> bool | None:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, int) and valor in (0, 1):
        return bool(valor)
    return None


def _convertir_dispositivo(crudo: dict, indice: int) -> DispositivoBaanool:
    problemas: list[str] = []
    name = crudo.get("name")
    if isinstance(name, str):
        name = name.strip()
        if not name:
            problemas.append(NAME_VACIO)
    else:
        problemas.append(NAME_VACIO if name is None else NAME_INVALIDO)
        name = ""
    dno, problema_dno = _dno_a_texto(crudo.get("dno"))
    if problema_dno:
        problemas.append(problema_dno)
    car_num = crudo.get("carNum")
    car_num = car_num.strip() if isinstance(car_num, str) and car_num.strip() else None
    return DispositivoBaanool(
        name=name, dno=dno, car_num=car_num, lat=_numero(crudo.get("lat")), lng=_numero(crudo.get("lng")),
        dev_status=crudo.get("devStatus"), online=_booleano(crudo.get("online")),
        problemas=tuple(problemas), indice=indice)


def obtener_dispositivos(token: str, sesion=None) -> ResultadoDispositivos:
    """Consulta los dispositivos de la cuenta (POST, cuerpo vacío). No descarga ningún GPX."""
    sesion = sesion or crear_sesion()
    try:
        respuesta = sesion.post(URL_DISPOSITIVOS, data=b"", headers=_encabezados_dispositivos(token),
                                timeout=TIMEOUT_BAANOOL_SEGUNDOS)
    except requests.exceptions.Timeout:
        raise ErrorBaanool(TIMEOUT, f"BAANOOL no respondió en {TIMEOUT_BAANOOL_SEGUNDOS} s "
                                    "(lista de dispositivos).") from None
    except requests.exceptions.RequestException as exc:
        raise ErrorBaanool(ERROR_RED, "Error de red al consultar los dispositivos: "
                                      f"{_sin_token(type(exc).__name__, token)}.") from None

    if respuesta.status_code in (401, 403):
        raise ErrorBaanool(TOKEN_INVALIDO, f"BAANOOL rechazó el token (HTTP {respuesta.status_code}): "
                                           "la sesión pudo vencer. Proporciona un token nuevo.")
    if respuesta.status_code != 200:
        raise ErrorBaanool(ERROR_HTTP, f"BAANOOL respondió HTTP {respuesta.status_code} "
                                       "a la consulta de dispositivos.")
    try:
        cuerpo = respuesta.json()
    except ValueError:
        raise ErrorBaanool(JSON_INVALIDO, "La respuesta de dispositivos no es un JSON válido "
                                          "(HTTP 200).") from None
    if not isinstance(cuerpo, dict):
        raise ErrorBaanool(JSON_INVALIDO, "La respuesta de dispositivos no tiene la estructura esperada.")

    status, resmsg = cuerpo.get("status"), _sin_token(str(cuerpo.get("resmsg")), token)
    if status == STATUS_DEMASIADAS_SOLICITUDES:
        raise ErrorBaanool(RATE_LIMIT_BAANOOL, "BAANOOL limitó temporalmente las solicitudes. "
                                               "Espera antes de volver a intentar.")
    if status == STATUS_SIN_PERMISO:
        raise ErrorBaanool(PERMISO_BAANOOL, f"BAANOOL respondió status={status}, resmsg={resmsg!r}: la "
                                            "solicitud no tiene permiso. No hay evidencia de que esto "
                                            "signifique sesión vencida.")
    if status != STATUS_EXITO:
        pista = " La sesión pudo vencer: proporciona un token nuevo." if status in (401, 403) else ""
        raise ErrorBaanool(STATUS_BAANOOL, f"BAANOOL respondió HTTP 200 pero status={status!r}, "
                                           f"resmsg={resmsg!r}.{pista}")

    datos = cuerpo.get("data")
    if datos is None:
        raise ErrorBaanool(DATA_AUSENTE, f"BAANOOL indicó éxito (status={status}) pero no devolvió 'data'.")
    contenedores = datos if isinstance(datos, list) else [datos]
    crudos = None
    for contenedor in contenedores:
        if isinstance(contenedor, dict) and isinstance(contenedor.get("devices"), list):
            crudos = (crudos or []) + contenedor["devices"]
    if crudos is None:
        raise ErrorBaanool(DISPOSITIVOS_AUSENTES, "La respuesta no contiene la lista 'devices'.")
    if not crudos:
        raise ErrorBaanool(DISPOSITIVOS_VACIOS, "BAANOOL devolvió una lista de dispositivos vacía "
                                                "(¿el token corresponde a la cuenta correcta?).")

    dispositivos: list[DispositivoBaanool] = []
    advertencias: list[str] = []
    for indice, crudo in enumerate(crudos):
        if not isinstance(crudo, dict):  # elemento mal formado: no tira toda la lista
            advertencias.append(f"El elemento {indice} de 'devices' no es un objeto y se ignoró.")
            continue
        dispositivos.append(_convertir_dispositivo(crudo, indice))
    return ResultadoDispositivos(respuesta.status_code, status, resmsg, dispositivos, advertencias)
