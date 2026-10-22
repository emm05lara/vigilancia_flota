"""Cálculos sobre listas de puntos. Todas las distancias son ESTIMADAS:
se obtienen de puntos GPX separados por minutos, no son kilometraje real.

Distancias que se manejan por separado (nunca se mezclan en una sola):
- distancia_estimada_observada: suma de tramos normales (seguimiento GPS continuo).
- distancia_ruido_paradas: pequeños desplazamientos entre extremos de paradas probables.
- desplazamiento_minimo_huecos: línea recta entre los extremos de cada hueco con
  desplazamiento. La trayectoria real entre esos puntos es desconocida.
- distancia_bruta: suma de las tres; solo para diagnóstico.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from statistics import median

from .gpx_parser import Punto
from .parametros import RADIO_PARADA_KM, TOLERANCIA_DUPLICADO_METROS, UMBRAL_HUECO_MINUTOS

RADIO_TIERRA_KM = 6371.0088
UMBRAL_HUECO = timedelta(minutes=UMBRAL_HUECO_MINUTOS)

TRAMO_NORMAL = "tramo_normal"
PARADA_PROBABLE = "parada_probable"
HUECO_CON_DESPLAZAMIENTO = "hueco_con_desplazamiento"


@dataclass(frozen=True)
class Tramo:
    """Intervalo entre dos puntos consecutivos."""
    inicio: datetime
    fin: datetime
    distancia_km: float  # distancia en línea recta entre los dos puntos
    duracion: timedelta
    velocidad_media_kmh: float | None  # None si la duración es 0
    tipo: str  # TRAMO_NORMAL | PARADA_PROBABLE | HUECO_CON_DESPLAZAMIENTO
    coordenada_inicial: tuple[float, float]
    coordenada_final: tuple[float, float]


@dataclass(frozen=True)
class ConflictoTimestamp:
    """Dos puntos con la misma fecha/hora pero en lugares distintos."""
    fecha: datetime
    coordenada_conservada: tuple[float, float]
    coordenada_excluida: tuple[float, float]
    distancia_m: float


@dataclass(frozen=True)
class Consolidacion:
    puntos: list[Punto]  # ordenados, una sola posición por fecha/hora
    duplicados_eliminados: int
    conflictos: list[ConflictoTimestamp]


@dataclass(frozen=True)
class ResumenPuntos:
    cantidad_puntos: int
    fecha_inicial: datetime | None
    fecha_final: datetime | None
    duracion: timedelta | None
    coordenada_inicial: tuple[float, float] | None
    coordenada_final: tuple[float, float] | None
    distancia_estimada_observada_km: float  # distancia principal
    distancia_ruido_paradas_km: float
    desplazamiento_minimo_huecos_km: float
    distancia_bruta_km: float  # diagnóstico: suma de las tres anteriores
    intervalo_minimo: timedelta | None
    intervalo_mediano: timedelta | None
    intervalo_maximo: timedelta | None
    velocidad_media_maxima_kmh: float | None  # solo entre tramos normales
    paradas_probables: list[Tramo]
    huecos_con_desplazamiento: list[Tramo]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * RADIO_TIERRA_KM * asin(sqrt(a))


def consolidar_puntos(puntos: list[Punto]) -> Consolidacion:
    """Ordena por fecha y deja una sola posición por fecha/hora.

    Misma fecha y a <= TOLERANCIA_DUPLICADO_METROS: duplicado, se elimina.
    Misma fecha y más lejos: conflicto; se conserva el primero (el orden de entrada
    decide) y el otro queda registrado en `conflictos`, fuera de los cálculos.
    """
    conservados: list[Punto] = []
    duplicados = 0
    conflictos: list[ConflictoTimestamp] = []

    for punto in sorted(puntos, key=lambda p: p.fecha):  # orden estable
        if conservados and conservados[-1].fecha == punto.fecha:
            previo = conservados[-1]
            metros = 1000 * haversine_km(previo.latitud, previo.longitud, punto.latitud, punto.longitud)
            if metros <= TOLERANCIA_DUPLICADO_METROS:
                duplicados += 1
            else:
                conflictos.append(ConflictoTimestamp(
                    fecha=punto.fecha,
                    coordenada_conservada=(previo.latitud, previo.longitud),
                    coordenada_excluida=(punto.latitud, punto.longitud),
                    distancia_m=metros,
                ))
            continue
        conservados.append(punto)

    return Consolidacion(conservados, duplicados, conflictos)


def clasificar_tramo(duracion: timedelta, distancia_km: float) -> str:
    if duracion <= UMBRAL_HUECO:
        return TRAMO_NORMAL
    if distancia_km <= RADIO_PARADA_KM:
        return PARADA_PROBABLE
    return HUECO_CON_DESPLAZAMIENTO


def calcular_tramos(puntos: list[Punto]) -> list[Tramo]:
    """Un tramo por cada par de puntos consecutivos (puntos ya consolidados y ordenados)."""
    tramos = []
    for a, b in zip(puntos, puntos[1:]):
        distancia = haversine_km(a.latitud, a.longitud, b.latitud, b.longitud)
        duracion = b.fecha - a.fecha
        horas = duracion.total_seconds() / 3600
        tramos.append(Tramo(
            inicio=a.fecha,
            fin=b.fecha,
            distancia_km=distancia,
            duracion=duracion,
            velocidad_media_kmh=distancia / horas if horas > 0 else None,
            tipo=clasificar_tramo(duracion, distancia),
            coordenada_inicial=(a.latitud, a.longitud),
            coordenada_final=(b.latitud, b.longitud),
        ))
    return tramos


def _distancia_de_tipo(tramos: list[Tramo], tipo: str) -> float:
    return sum(t.distancia_km for t in tramos if t.tipo == tipo)


def resumir_puntos(puntos: list[Punto]) -> ResumenPuntos:
    """Métricas de una lista de puntos ya consolidados (ver consolidar_puntos)."""
    if not puntos:
        return ResumenPuntos(0, None, None, None, None, None, 0.0, 0.0, 0.0, 0.0,
                             None, None, None, None, [], [])

    primero, ultimo = puntos[0], puntos[-1]
    tramos = calcular_tramos(puntos)
    intervalos = [t.duracion for t in tramos]
    velocidades = [t.velocidad_media_kmh for t in tramos
                   if t.tipo == TRAMO_NORMAL and t.velocidad_media_kmh is not None]

    observada = _distancia_de_tipo(tramos, TRAMO_NORMAL)
    ruido = _distancia_de_tipo(tramos, PARADA_PROBABLE)
    huecos = _distancia_de_tipo(tramos, HUECO_CON_DESPLAZAMIENTO)

    return ResumenPuntos(
        cantidad_puntos=len(puntos),
        fecha_inicial=primero.fecha,
        fecha_final=ultimo.fecha,
        duracion=ultimo.fecha - primero.fecha,
        coordenada_inicial=(primero.latitud, primero.longitud),
        coordenada_final=(ultimo.latitud, ultimo.longitud),
        distancia_estimada_observada_km=observada,
        distancia_ruido_paradas_km=ruido,
        desplazamiento_minimo_huecos_km=huecos,
        distancia_bruta_km=observada + ruido + huecos,
        intervalo_minimo=min(intervalos) if intervalos else None,
        intervalo_mediano=median(intervalos) if intervalos else None,
        intervalo_maximo=max(intervalos) if intervalos else None,
        velocidad_media_maxima_kmh=max(velocidades) if velocidades else None,
        paradas_probables=[t for t in tramos if t.tipo == PARADA_PROBABLE],
        huecos_con_desplazamiento=[t for t in tramos if t.tipo == HUECO_CON_DESPLAZAMIENTO],
    )
