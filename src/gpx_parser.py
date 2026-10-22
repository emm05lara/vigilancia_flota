"""Lectura de GPX exportados de BAANOOL."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ZONA_HORARIA = ZoneInfo("America/Mexico_City")


@dataclass(frozen=True)
class Punto:
    fecha: datetime  # con zona horaria
    latitud: float
    longitud: float
    elevacion: float


def _nombre_local(etiqueta: str) -> str:
    """'{ns}trkpt' -> 'trkpt' (por si algún GPX trae namespace)."""
    return etiqueta.rsplit("}", 1)[-1]


def _parsear_fecha(texto: str) -> datetime:
    fecha = datetime.fromisoformat(texto.strip().replace("Z", "+00:00"))
    if fecha.tzinfo is None:  # BAANOOL no indica zona: se asume hora local de México
        fecha = fecha.replace(tzinfo=ZONA_HORARIA)
    return fecha


def _parsear_punto(elemento) -> Punto:
    hijos = {_nombre_local(h.tag): (h.text or "") for h in elemento}
    return Punto(
        fecha=_parsear_fecha(hijos["time"]),
        latitud=float(elemento.attrib["lat"]),
        longitud=float(elemento.attrib["lon"]),
        elevacion=float(hijos.get("ele") or 0.0),
    )


def leer_gpx(ruta: Path) -> tuple[list[Punto], int]:
    """Devuelve (puntos ordenados cronológicamente, cantidad de puntos descartados).

    Un punto sin fecha o con coordenadas no numéricas se descarta y se cuenta.
    Si el XML no es válido lanza ET.ParseError.
    """
    puntos: list[Punto] = []
    descartados = 0
    for _evento, elem in ET.iterparse(ruta):
        if _nombre_local(elem.tag) != "trkpt":
            continue
        try:
            puntos.append(_parsear_punto(elem))
        except (KeyError, ValueError):
            descartados += 1
        elem.clear()
    puntos.sort(key=lambda p: p.fecha)
    return puntos, descartados
