"""Manifiesto local del resultado de adquisición (etapa 6C): solo METADATOS, un JSON por periodo.

Motivo: SIN_DATOS_PERIODO no deja ningún GPX en entrada/ (a propósito, para poder consultar de nuevo).
Sin un registro, el análisis que solo mira entrada/ confundiría "BAANOOL respondió sin posiciones" con
"vehículo nunca consultado" (SIN_GPX) o con "sin dispositivo en BAANOOL" (SOLO_LOCAL).

Semántica que se conserva:
- SIN_DATOS_PERIODO: el dispositivo existe y BAANOOL respondió para ese periodo, pero no entregó
  posiciones. La causa es desconocida y no se infiere.
- SOLO_LOCAL: la LLAVE existe localmente pero no tiene dispositivo BAANOOL exacto.
- estado_actual_baanool (ONLINE/OFFLINE): fotografía de la consulta; NO describe el periodo histórico
  y NO determina ningún estado de adquisición.

El manifiesto NO participa en la decisión de pedir un GPX: esa lógica (descarga_lote) no lo lee. Un
SIN_DATOS_PERIODO registrado ayer no equivale a YA_EXISTE y puede consultarse de nuevo.

No contiene token, cookies, headers, URLs de descarga, DNO ni rutas absolutas. Se escribe de forma
atómica (archivo temporal + reemplazo). El nombre usa el periodo LOCAL (America/Mexico_City).
"""

import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .descarga_lote import (DESCARGADO, LOTE_COMPLETADO, LOTE_DRY_RUN, YA_EXISTE, ResultadoLote)
from .mapeo_baanool import SOLO_BAANOOL, ResultadoMapeoBaanool
from .parametros import ZONA_HORARIA_LOCAL

VERSION_FORMATO = 1
CARPETA_MANIFIESTOS = Path(__file__).resolve().parents[1] / "salida" / "estado_descargas"
NO_PROCESADO = "NO_PROCESADO"  # exacta que no se procesó en la corrida (lote detenido o --limite)

NOTA_ESTADO_ACTUAL = ("estado_actual_baanool (ONLINE/OFFLINE) es la fotografía observada al consultar "
                      "BAANOOL; no describe el periodo histórico ni determina SIN_DATOS_PERIODO.")
_RUTA_ABSOLUTA_WINDOWS = re.compile(r"[A-Za-z]:\\[^\s'\"]*")


class ManifiestoInvalido(ValueError):
    """El archivo existe pero no se puede interpretar como un manifiesto de esta versión."""


class ErrorEscrituraManifiesto(OSError):
    """No se pudo escribir el manifiesto; el anterior (si existía) queda intacto."""


# ---------- nombre y ubicación ----------

def nombre_manifiesto(inicio: datetime, fin: datetime) -> str:
    """2026-10-10_000000__2026-10-10_235959.json (periodo LOCAL solicitado)."""
    return f"{inicio:%Y-%m-%d_%H%M%S}__{fin:%Y-%m-%d_%H%M%S}.json"


def ruta_manifiesto(inicio: datetime, fin: datetime, carpeta: Path | None = None) -> Path:
    return (carpeta or CARPETA_MANIFIESTOS) / nombre_manifiesto(inicio, fin)


# ---------- saneamiento ----------

def _relativa(ruta: Path | None, raiz: Path) -> str | None:
    """Ruta relativa al proyecto con '/' (portable). Nunca una ruta absoluta."""
    if ruta is None:
        return None
    try:
        return ruta.resolve().relative_to(raiz.resolve()).as_posix()
    except ValueError:
        try:
            return ruta.relative_to(raiz).as_posix()
        except ValueError:
            return f"{ruta.parent.name}/{ruta.name}"  # última salvaguardia: sin rutas absolutas


def _texto_limpio(texto: str | None, raiz: Path) -> str | None:
    """Quita del texto cualquier ruta absoluta (la del proyecto y cualquier 'X:\\...')."""
    if not texto:
        return None
    for variante in {str(raiz), str(raiz.resolve())}:
        texto = texto.replace(variante + os.sep, "").replace(variante, "")
    return _RUTA_ABSOLUTA_WINDOWS.sub("<ruta>", texto)


# ---------- construcción ----------

def _entrada(llave: str, estado: str, ahora: str, *, actual: str | None = None, archivo: str | None = None,
             puntos: int | None = None, mensaje: str | None = None) -> dict:
    return {"llave": llave, "estado_adquisicion": estado, "estado_actual_baanool": actual,
            "archivo_gpx": archivo, "cantidad_puntos": puntos, "mensaje": mensaje,
            "ultima_actualizacion": ahora}


def construir_manifiesto(lote: ResultadoLote, mapeo: ResultadoMapeoBaanool, raiz_proyecto: Path,
                         ahora: datetime, previo: dict | None = None) -> dict:
    """Arma el diccionario del manifiesto. `previo` (si existe) aporta el estado de las exactas que
    NO se procesaron en esta corrida, para no sustituir un resultado conocido por "no procesado"."""
    if lote.estado == LOTE_DRY_RUN:
        raise ValueError("Un dry-run no genera manifiesto")
    marca = ahora.isoformat(timespec="seconds")
    previas = {v["llave"]: v for v in (previo or {}).get("vehiculos", []) if isinstance(v, dict)}
    procesadas = {v.llave: v for v in lote.vehiculos}
    exactas = mapeo.exactas

    motivo_corte = None
    if lote.estado != LOTE_COMPLETADO:
        motivo_corte = _texto_limpio(lote.motivo_detencion, raiz_proyecto) or lote.estado
    elif lote.seleccionadas < lote.exactas_totales:
        motivo_corte = (f"--limite: se procesaron {lote.seleccionadas} de {lote.exactas_totales} "
                        "coincidencias exactas")
    lote_completo = motivo_corte is None

    vehiculos: dict[str, dict] = {}
    for entrada in mapeo.entradas:  # una por LLAVE local (más las que solo existen en BAANOOL)
        if entrada.llave is None:
            continue
        llave = entrada.llave
        if llave in exactas:
            actual = None
            if exactas[llave].online is not None:
                actual = "ONLINE" if exactas[llave].online else "OFFLINE"
            if llave in procesadas:
                v = procesadas[llave]
                con_archivo = v.estado in (DESCARGADO, YA_EXISTE)
                vehiculos[llave] = _entrada(
                    llave, v.estado, marca, actual=v.estado_actual_baanool or actual,
                    archivo=_relativa(v.ruta_archivo, raiz_proyecto) if con_archivo else None,
                    puntos=v.cantidad_puntos, mensaje=_texto_limpio(v.mensaje, raiz_proyecto))
            elif llave in previas and previas[llave].get("estado_adquisicion") != NO_PROCESADO:
                vehiculos[llave] = dict(previas[llave])  # se conserva lo ya conocido
            else:
                vehiculos[llave] = _entrada(llave, NO_PROCESADO, marca, actual=actual,
                                            mensaje=motivo_corte or "No se procesó en esta corrida")
        else:  # SOLO_LOCAL, POSIBLE_COINCIDENCIA o CONFLICTO: nunca entraron al lote BAANOOL
            vehiculos[llave] = _entrada(llave, entrada.estado, marca,
                                        mensaje=_texto_limpio(entrada.motivo, raiz_proyecto))

    lista = [vehiculos[k] for k in sorted(vehiculos)]
    por_estado = Counter(v["estado_adquisicion"] for v in lista)
    return {
        "version_formato": VERSION_FORMATO,
        "periodo_inicio_local": lote.inicio.strftime("%Y-%m-%d %H:%M:%S"),
        "periodo_fin_local": lote.fin.strftime("%Y-%m-%d %H:%M:%S"),
        "zona_horaria_local": ZONA_HORARIA_LOCAL,
        "fecha_ejecucion": marca,
        "lote_completo": lote_completo,
        "motivo_interrupcion": motivo_corte,
        "nota": NOTA_ESTADO_ACTUAL,
        "resumen": {
            "llaves_locales": len(lista),
            "dispositivos_baanool": len(mapeo.dispositivos),
            "solo_baanool_sin_llave": sum(1 for e in mapeo.entradas if e.estado == SOLO_BAANOOL),
            "por_estado": dict(sorted(por_estado.items())),
        },
        "vehiculos": lista,
    }


# ---------- escritura atómica y lectura ----------

def escribir_manifiesto(manifiesto: dict, destino: Path) -> Path:
    """Archivo temporal -> cierre -> reemplazo atómico. Un fallo no toca un manifiesto válido previo."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(destino.name + ".tmp")
    try:
        with open(temporal, "w", encoding="utf-8") as archivo:
            json.dump(manifiesto, archivo, ensure_ascii=False, indent=2)
            archivo.write("\n")
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, destino)
    except Exception as exc:
        try:
            temporal.unlink(missing_ok=True)
        except OSError:
            pass
        raise ErrorEscrituraManifiesto(f"No se pudo escribir el manifiesto ({type(exc).__name__})") from None
    return destino


def cargar_manifiesto_adquisicion(inicio: datetime, fin: datetime, carpeta: Path | None = None) -> dict | None:
    """Devuelve el manifiesto del periodo, o None si no existe. Si existe pero no es legible o es de otra
    versión lanza ManifiestoInvalido (no se inventan estados)."""
    ruta = ruta_manifiesto(inicio, fin, carpeta)
    if not ruta.is_file():
        return None
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifiestoInvalido(f"El manifiesto {ruta.name} no es un JSON legible ({type(exc).__name__})") from None
    if not isinstance(datos, dict) or datos.get("version_formato") != VERSION_FORMATO \
            or not isinstance(datos.get("vehiculos"), list):
        raise ManifiestoInvalido(f"El manifiesto {ruta.name} tiene un formato o una versión no reconocidos")
    return datos


def estados_por_llave(manifiesto: dict) -> dict[str, dict]:
    """{llave -> entrada} para consultar el estado de adquisición de una LLAVE."""
    return {v["llave"]: v for v in manifiesto["vehiculos"]}


def actualizar_manifiesto(lote: ResultadoLote, mapeo: ResultadoMapeoBaanool, raiz_proyecto: Path,
                          carpeta: Path | None = None, ahora: datetime | None = None) -> Path:
    """Genera/actualiza el manifiesto del periodo del lote. Reemplaza el estado de cada vehículo
    procesado (nunca acumula estados contradictorios) y conserva el de los no procesados."""
    ahora = ahora or datetime.now(ZoneInfo(ZONA_HORARIA_LOCAL))
    try:
        previo = cargar_manifiesto_adquisicion(lote.inicio, lote.fin, carpeta)
    except ManifiestoInvalido:
        previo = None  # un manifiesto ilegible no se hereda; se reemplaza de forma atómica
    manifiesto = construir_manifiesto(lote, mapeo, raiz_proyecto, ahora, previo)
    return escribir_manifiesto(manifiesto, ruta_manifiesto(lote.inicio, lote.fin, carpeta))
