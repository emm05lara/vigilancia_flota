"""Clasificación geográfica de puntos: DENTRO / TOLERANCIA / FUERA de la zona permitida.

Las geometrías salen del Marco Geoestadístico de INEGI (áreas geoestadísticas estatales),
guardadas localmente por preparar_zonas.py. Son una referencia geográfica operativa,
no una determinación jurídica exacta de límites político-administrativos.

Este módulo solo clasifica puntos; la detección de eventos está en geocercas.py.
"""

import json
from dataclasses import dataclass, field
from math import cos, radians
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from .gpx_parser import Punto
from .parametros import CLAVES_ENTIDADES_PERMITIDAS, TOLERANCIA_FRONTERA_METROS

DENTRO = "dentro"
TOLERANCIA = "tolerancia"
FUERA = "fuera"

METROS_POR_GRADO = 111_195  # radio medio de la Tierra * pi / 180
ARCHIVO_ENTIDADES = "entidades_mexico.geojson"


@dataclass(frozen=True)
class Entidad:
    clave: str
    nombre: str
    geometria: object  # shapely Polygon | MultiPolygon, en lon/lat


@dataclass
class Zonas:
    """Entidades federativas y la zona permitida (unión de las entidades permitidas)."""
    entidades: list[Entidad]
    claves_permitidas: tuple[str, ...] = CLAVES_ENTIDADES_PERMITIDAS
    tolerancia_metros: float = TOLERANCIA_FRONTERA_METROS

    permitida: object = field(init=False, repr=False)
    _permitida_preparada: object = field(init=False, repr=False)
    _permitida_metrica: object = field(init=False, repr=False)
    _escala: np.ndarray = field(init=False, repr=False)
    _arbol: shapely.STRtree = field(init=False, repr=False)

    def __post_init__(self):
        permitidas = [e.geometria for e in self.entidades if e.clave in self.claves_permitidas]
        if not permitidas:
            raise ValueError(f"Ninguna entidad coincide con las claves permitidas {self.claves_permitidas}")
        self.permitida = unary_union(permitidas)
        self._permitida_preparada = self.permitida
        shapely.prepare(self._permitida_preparada)

        # Distancias en metros con una proyección equirectangular local: se usa el
        # coseno de la latitud central de la zona permitida. En el área de interés
        # (+-1 grado de latitud) el error es de ~1 %, irrelevante para 100 m.
        latitud_central = self.permitida.centroid.y
        self._escala = np.array([cos(radians(latitud_central)), 1.0]) * METROS_POR_GRADO
        self._permitida_metrica = shapely.transform(self.permitida, lambda c: c * self._escala)

        self._arbol = shapely.STRtree([e.geometria for e in self.entidades])

    def entidad_en(self, punto: Point) -> Entidad | None:
        indices = self._arbol.query(punto, predicate="intersects")
        if len(indices) == 0:
            return None
        return self.entidades[int(min(indices))]  # el menor índice, para que sea determinista

    def distancia_a_zona_metros(self, longitud: float, latitud: float) -> float:
        x, y = np.array([longitud, latitud]) * self._escala
        return self._permitida_metrica.distance(Point(x, y))

    def contiene(self, punto: Point) -> bool:
        return self._permitida_preparada.intersects(punto)  # el borde cuenta como dentro


@dataclass(frozen=True)
class PuntoClasificado:
    punto: Punto  # el punto original, sin modificar
    estado: str  # DENTRO | TOLERANCIA | FUERA
    clave_entidad: str | None  # None: el punto no cae en ninguna entidad (p. ej. mar o extranjero)
    entidad: str | None
    distancia_a_zona_m: float | None  # solo si no está DENTRO

    @property
    def dentro_zona_permitida(self) -> bool:
        return self.estado == DENTRO


def clasificar_punto(punto: Punto, zonas: Zonas) -> PuntoClasificado:
    geo = Point(punto.longitud, punto.latitud)
    entidad = zonas.entidad_en(geo)
    clave = entidad.clave if entidad else None
    nombre = entidad.nombre if entidad else None

    if zonas.contiene(geo):
        return PuntoClasificado(punto, DENTRO, clave, nombre, None)

    distancia = zonas.distancia_a_zona_metros(punto.longitud, punto.latitud)
    estado = TOLERANCIA if distancia <= zonas.tolerancia_metros else FUERA
    return PuntoClasificado(punto, estado, clave, nombre, distancia)


def clasificar_puntos(puntos: list[Punto], zonas: Zonas) -> list[PuntoClasificado]:
    return [clasificar_punto(p, zonas) for p in puntos]


def cargar_zonas(carpeta: Path) -> Zonas:
    """Carga las entidades desde el GeoJSON local. Nunca descarga nada."""
    archivo = carpeta / ARCHIVO_ENTIDADES
    if not archivo.is_file():
        raise FileNotFoundError(
            f"Falta {archivo}. Ejecuta una sola vez 'py preparar_zonas.py' para obtenerlo "
            f"del Marco Geoestadístico de INEGI.")
    datos = json.loads(archivo.read_text(encoding="utf-8"))
    entidades = [
        Entidad(f["properties"]["cve_ent"], f["properties"]["nombre"], shape(f["geometry"]))
        for f in datos["features"]
    ]
    return Zonas(entidades)
