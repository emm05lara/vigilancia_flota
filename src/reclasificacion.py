"""Revisión de archivos *.gpx.invalido históricos (etapa 6B-2, cierre).

Antes de distinguir SIN_DATOS_PERIODO, un GPX descargado sin posiciones se guardaba como
`*.gpx.invalido`. Este módulo decide, para cada uno, qué es en realidad:

- SIN_DATOS_PERIODO_HISTORICO: XML estructuralmente válido, raíz <gpx>, con 0 <trkpt>. Significa
  únicamente que BAANOOL respondió sin posiciones para ese dispositivo y periodo; la causa es
  desconocida y no se infiere.
- GPX_INVALIDO: XML corrupto, que no es GPX, o con <trkpt> de los que ninguno es válido.
- CON_PUNTOS_VALIDOS: inesperado (tenía posiciones válidas); no se mueve, se reporta para revisión.

Los SIN_DATOS_PERIODO históricos se MUEVEN (nunca se borran) a una carpeta de diagnóstico para que no
bloqueen una nueva consulta; se conserva la subcarpeta del vehículo (que identifica la LLAVE) y el
nombre original del archivo (que identifica el periodo). Nunca se sobrescribe: una colisión se reporta.

Solo se tocan archivos cuyo nombre termina en `.gpx.invalido`.
"""

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .gpx_parser import leer_gpx

SIN_DATOS_PERIODO_HISTORICO = "SIN_DATOS_PERIODO_HISTORICO"
GPX_INVALIDO = "GPX_INVALIDO"
CON_PUNTOS_VALIDOS = "CON_PUNTOS_VALIDOS"

# Acción sobre el archivo
MOVER = "MOVER"  # se movería (dry-run) o está pendiente
MOVIDO = "MOVIDO"
CONSERVAR = "CONSERVAR"  # se deja donde está
COLISION = "COLISION"  # ya hay algo con ese nombre en el destino: no se toca nada
ERROR_AL_MOVER = "ERROR_AL_MOVER"

SUFIJO = ".gpx.invalido"


@dataclass(frozen=True)
class ItemReclasificacion:
    origen: Path
    carpeta: str  # nombre de la carpeta del vehículo (derivado de la LLAVE)
    clasificacion: str
    detalle: str
    accion: str
    destino: Path | None = None


def _nombre_local(etiqueta: str) -> str:
    return etiqueta.rsplit("}", 1)[-1].lower()


def clasificar_gpx_invalido(ruta: Path) -> tuple[str, str]:
    """(clasificación, detalle) de un archivo *.gpx.invalido. Solo lee; no modifica nada."""
    try:
        raiz = ET.parse(ruta).getroot()
    except ET.ParseError as exc:
        return GPX_INVALIDO, f"XML corrupto o incompleto ({type(exc).__name__})"
    except OSError as exc:
        return GPX_INVALIDO, f"No se pudo leer el archivo ({type(exc).__name__})"

    if _nombre_local(raiz.tag) != "gpx":
        return GPX_INVALIDO, f"El XML no es un GPX (raíz <{_nombre_local(raiz.tag)}>)"

    trkpt = sum(1 for elemento in raiz.iter() if _nombre_local(elemento.tag) == "trkpt")
    if trkpt == 0:
        return SIN_DATOS_PERIODO_HISTORICO, "GPX estructuralmente válido con 0 puntos (trkpt)"

    puntos, descartados = leer_gpx(ruta)  # el parser del proyecto decide la validez de cada punto
    if puntos:
        return CON_PUNTOS_VALIDOS, f"Contiene {len(puntos)} punto(s) válido(s): no debería estar marcado inválido"
    return GPX_INVALIDO, f"Contiene {trkpt} trkpt pero ninguno es válido ({descartados} descartados)"


def encontrar_gpx_invalidos(carpeta_entrada: Path) -> list[Path]:
    """Solo *.gpx.invalido dentro de las carpetas de vehículo (un nivel), en orden determinista."""
    encontrados = []
    for carpeta in sorted(p for p in carpeta_entrada.iterdir() if p.is_dir()):
        encontrados.extend(sorted(p for p in carpeta.iterdir()
                                  if p.is_file() and p.name.lower().endswith(SUFIJO)))
    return encontrados


def reclasificar(carpeta_entrada: Path, carpeta_diagnostico: Path, dry_run: bool) -> list[ItemReclasificacion]:
    """Clasifica cada *.gpx.invalido y, si no es dry_run, mueve los SIN_DATOS_PERIODO históricos."""
    items: list[ItemReclasificacion] = []
    for ruta in encontrar_gpx_invalidos(carpeta_entrada):
        clasificacion, detalle = clasificar_gpx_invalido(ruta)
        carpeta = ruta.parent.name

        if clasificacion != SIN_DATOS_PERIODO_HISTORICO:
            items.append(ItemReclasificacion(ruta, carpeta, clasificacion, detalle, CONSERVAR))
            continue

        destino = carpeta_diagnostico / carpeta / ruta.name
        if destino.exists():  # nunca se sobrescribe ni se inventa un sufijo
            items.append(ItemReclasificacion(
                ruta, carpeta, clasificacion, detalle + "; ya existe un archivo con ese nombre en el destino",
                COLISION, destino))
            continue
        if dry_run:
            items.append(ItemReclasificacion(ruta, carpeta, clasificacion, detalle, MOVER, destino))
            continue
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(ruta), str(destino))
        except OSError as exc:
            items.append(ItemReclasificacion(ruta, carpeta, clasificacion,
                                             f"{detalle}; no se pudo mover ({type(exc).__name__})",
                                             ERROR_AL_MOVER, destino))
            continue
        items.append(ItemReclasificacion(ruta, carpeta, clasificacion, detalle, MOVIDO, destino))
    return items
