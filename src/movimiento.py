"""Trayectos y resumen de actividad. Solo cálculo; no depende de Folium.

Un trayecto es una secuencia de puntos unidos por tramos normales (<= UMBRAL_HUECO_MINUTOS).
Lo corta cualquier intervalo largo: una parada probable o un hueco con desplazamiento
(en un hueco no se sabe por dónde fue el vehículo, así que no se une).
Secuencias con menos de DISTANCIA_MINIMA_TRAYECTO_KM se descartan: es ruido GPS.

La distancia de un trayecto es solo la de sus tramos normales (distancia estimada
observada); nunca incluye líneas rectas a través de huecos.

La duración de un trayecto es el periodo de actividad reportado, NO tiempo en movimiento:
BAANOOL puede seguir reportando cada ~5 min aunque el vehículo no se esté desplazando.

Un intervalo largo entre dos puntos no demuestra por sí mismo que el vehículo estuvo detenido: puede
ser también una pérdida temporal de reporte o de señal. Por eso solo se habla de "parada probable"
(extremos cercanos) y de "hueco con desplazamiento" (extremos lejanos, trayectoria desconocida).

LIMITACIÓN CONOCIDA (esta etapa): una parada probable solo se detecta cuando hay un
intervalo > UMBRAL_HUECO_MINUTOS con extremos cercanos. Si el vehículo no se desplaza pero
BAANOOL sigue enviando puntos cada ~5 min, esa parada NO se detecta (queda dentro de un
trayecto). Detectarla requeriría agrupar puntos cercanos; queda para una etapa posterior.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .gpx_parser import ZONA_HORARIA, Punto
from .metricas import PARADA_PROBABLE, HUECO_CON_DESPLAZAMIENTO, TRAMO_NORMAL, Tramo, calcular_tramos
from .parametros import DISTANCIA_MINIMA_TRAYECTO_KM


@dataclass(frozen=True)
class Trayecto:
    inicio: datetime
    fin: datetime
    duracion: timedelta
    cantidad_puntos: int
    distancia_estimada_observada_km: float
    coordenada_inicial: tuple[float, float]
    coordenada_final: tuple[float, float]
    puntos: tuple[Punto, ...]


@dataclass(frozen=True)
class ResumenActividad:
    cantidad_paradas_probables: int
    tiempo_en_paradas_probables: timedelta
    cantidad_huecos_con_desplazamiento: int
    # Distancia estimada observada (solo tramos normales) por día calendario en America/Mexico_City.
    # Incluye todos los días entre el primer y el último punto (0.0 si no hubo distancia observada).
    distancia_por_dia: dict[date, float]
    primera_actividad: datetime | None  # inicio del primer trayecto
    ultima_actividad: datetime | None  # fin del último trayecto
    trayectos: list[Trayecto]


def _crear_trayecto(puntos: list[Punto], tramos: list[Tramo]) -> Trayecto:
    return Trayecto(
        inicio=puntos[0].fecha,
        fin=puntos[-1].fecha,
        duracion=puntos[-1].fecha - puntos[0].fecha,
        cantidad_puntos=len(puntos),
        distancia_estimada_observada_km=sum(t.distancia_km for t in tramos),
        coordenada_inicial=(puntos[0].latitud, puntos[0].longitud),
        coordenada_final=(puntos[-1].latitud, puntos[-1].longitud),
        puntos=tuple(puntos),
    )


def construir_trayectos(puntos: list[Punto], tramos: list[Tramo]) -> list[Trayecto]:
    """puntos ordenados y tramos = calcular_tramos(puntos)."""
    if not puntos:
        return []

    trayectos = []
    puntos_grupo = [puntos[0]]
    tramos_grupo: list[Tramo] = []

    def cerrar_grupo() -> None:
        if tramos_grupo and sum(t.distancia_km for t in tramos_grupo) >= DISTANCIA_MINIMA_TRAYECTO_KM:
            trayectos.append(_crear_trayecto(puntos_grupo, tramos_grupo))

    for tramo, punto_siguiente in zip(tramos, puntos[1:]):
        if tramo.tipo == TRAMO_NORMAL:
            puntos_grupo.append(punto_siguiente)
            tramos_grupo.append(tramo)
        else:  # parada probable o hueco con desplazamiento: se cierra el grupo
            cerrar_grupo()
            puntos_grupo = [punto_siguiente]
            tramos_grupo = []
    cerrar_grupo()
    return trayectos


def _repartir_tramo_por_dia(tramo: Tramo, acumulado: dict[date, float]) -> None:
    """Reparte la distancia del tramo entre los días que toca, proporcional al tiempo en cada día.

    Un tramo normal dura <= 15 min, así que toca como máximo dos días; el reparto es
    determinista y la suma de los días es exactamente la distancia del tramo (no se duplica).
    """
    inicio = tramo.inicio.astimezone(ZONA_HORARIA)
    fin = tramo.fin.astimezone(ZONA_HORARIA)
    total = fin.timestamp() - inicio.timestamp()
    if total <= 0 or inicio.date() == fin.date():
        acumulado[inicio.date()] = acumulado.get(inicio.date(), 0.0) + tramo.distancia_km
        return

    dia = inicio.date()
    desde = inicio.timestamp()
    while dia <= fin.date():
        medianoche = datetime.combine(dia + timedelta(days=1), time.min, tzinfo=ZONA_HORARIA).timestamp()
        hasta = min(medianoche, fin.timestamp())
        acumulado[dia] = acumulado.get(dia, 0.0) + tramo.distancia_km * (hasta - desde) / total
        desde = hasta
        dia += timedelta(days=1)


def distancia_por_dia(puntos: list[Punto], tramos: list[Tramo]) -> dict[date, float]:
    """Distancia estimada observada por día calendario (hora de Ciudad de México).

    Solo tramos normales, igual que distancia_estimada_observada: la suma de los días
    coincide con ese total (salvo redondeo numérico).
    """
    if not puntos:
        return {}
    primer = puntos[0].fecha.astimezone(ZONA_HORARIA).date()
    ultimo = puntos[-1].fecha.astimezone(ZONA_HORARIA).date()
    acumulado = {primer + timedelta(days=i): 0.0 for i in range((ultimo - primer).days + 1)}
    for tramo in tramos:
        if tramo.tipo == TRAMO_NORMAL:
            _repartir_tramo_por_dia(tramo, acumulado)
    return dict(sorted(acumulado.items()))


def resumir_actividad(puntos: list[Punto]) -> ResumenActividad:
    tramos = calcular_tramos(puntos)
    trayectos = construir_trayectos(puntos, tramos)
    paradas = [t for t in tramos if t.tipo == PARADA_PROBABLE]
    return ResumenActividad(
        cantidad_paradas_probables=len(paradas),
        tiempo_en_paradas_probables=sum((t.duracion for t in paradas), timedelta()),
        cantidad_huecos_con_desplazamiento=sum(1 for t in tramos if t.tipo == HUECO_CON_DESPLAZAMIENTO),
        distancia_por_dia=distancia_por_dia(puntos, tramos),
        primera_actividad=trayectos[0].inicio if trayectos else None,
        ultima_actividad=trayectos[-1].fin if trayectos else None,
        trayectos=trayectos,
    )
