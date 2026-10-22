"""Eventos de salida de la zona permitida a partir de puntos ya clasificados.

Un evento es un periodo continuo en el que el vehículo estuvo fuera de la zona, entre
dos puntos DENTRO (o entre un extremo del GPX y un punto DENTRO).

INCERTIDUMBRE: los puntos llegan cada ~5 min o menos seguido. Si un punto está DENTRO y
el siguiente FUERA, el cruce ocurrió en algún momento entre ambos; aquí nunca se calcula
una hora exacta de cruce. Lo mismo para el regreso. Durante un hueco con desplazamiento
la trayectoria es desconocida y tampoco se interpola.

Puntos en TOLERANCIA (ruido cerca del límite) no cuentan como FUERA ni abren una salida
por sí solos, pero se conservan en `puntos_periodo` para diagnóstico.
"""

from dataclasses import dataclass
from datetime import timedelta

from .metricas import HUECO_CON_DESPLAZAMIENTO, calcular_tramos
from .movimiento import Trayecto
from .parametros import PUNTOS_MINIMOS_FUERA
from .zonas import DENTRO, FUERA, TOLERANCIA, PuntoClasificado, Zonas, clasificar_puntos
from .gpx_parser import Punto

SIN_ENTIDAD = "Sin entidad identificada"


@dataclass(frozen=True)
class EventoSalida:
    confirmado: bool  # al menos PUNTOS_MINIMOS_FUERA puntos FUERA consecutivos
    inicia_fuera: bool  # el GPX empieza fuera: la salida no fue observada
    abierta: bool  # el GPX termina fuera: no hay regreso observado

    ultimo_punto_dentro: PuntoClasificado | None  # None si inicia_fuera
    primer_punto_fuera: PuntoClasificado
    ultimo_punto_fuera: PuntoClasificado
    primer_punto_dentro_posterior: PuntoClasificado | None  # None si abierta

    entidades: tuple[str, ...]  # en orden de aparición
    claves_entidades: tuple[str, ...]
    cantidad_puntos_fuera: int
    cantidad_puntos_tolerancia: int
    puntos_periodo: tuple[PuntoClasificado, ...]  # todos los puntos no DENTRO del evento

    salida_con_hueco: bool  # entre último dentro y primer fuera hay un hueco con desplazamiento
    regreso_con_hueco: bool  # entre último fuera y primer dentro posterior hay uno
    huecos_durante_evento: int  # huecos con desplazamiento entre puntos FUERA
    duracion_observable_fuera: timedelta  # solo entre puntos FUERA consecutivos observados
    duracion_maxima_posible: timedelta | None  # último dentro -> primer dentro; None si no se conoce

    @property
    def es_salida_confirmada(self) -> bool:
        """Salida observada (dentro -> fuera) con suficientes puntos fuera."""
        return self.confirmado and not self.inicia_fuera

    @property
    def incertidumbre_salida(self) -> timedelta | None:
        """Ventana en la que ocurrió el cruce de salida (no es la hora del cruce)."""
        if self.ultimo_punto_dentro is None:
            return None
        return self.primer_punto_fuera.punto.fecha - self.ultimo_punto_dentro.punto.fecha

    @property
    def incertidumbre_regreso(self) -> timedelta | None:
        if self.primer_punto_dentro_posterior is None:
            return None
        return self.primer_punto_dentro_posterior.punto.fecha - self.ultimo_punto_fuera.punto.fecha


@dataclass(frozen=True)
class ResumenGeofencing:
    puntos_dentro: int
    puntos_tolerancia: int
    puntos_fuera: int
    eventos: list[EventoSalida]
    salidas_confirmadas: int  # dentro -> fuera observadas y confirmadas
    salidas_no_confirmadas: int  # menos de PUNTOS_MINIMOS_FUERA consecutivos (diagnóstico)
    inicia_fuera: bool  # el GPX empieza con un periodo fuera confirmado
    termino_fuera: bool  # el último punto está FUERA
    entidades_fuera: tuple[str, ...]  # entidades de los eventos confirmados
    clasificados: list[PuntoClasificado]

    @property
    def tuvo_salida_confirmada(self) -> bool:
        return self.salidas_confirmadas > 0


def _nombres_entidades(puntos_fuera: list[PuntoClasificado]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    nombres: dict[str, str] = {}
    for p in puntos_fuera:
        clave = p.clave_entidad or SIN_ENTIDAD
        nombres.setdefault(clave, p.entidad or SIN_ENTIDAD)
    return tuple(nombres.values()), tuple(c for c in nombres if c != SIN_ENTIDAD)


def _racha_maxima_fuera(periodo: list[PuntoClasificado]) -> int:
    mejor = actual = 0
    for p in periodo:
        actual = actual + 1 if p.estado == FUERA else 0
        mejor = max(mejor, actual)
    return mejor


def detectar_eventos(clasificados: list[PuntoClasificado],
                     puntos_minimos_fuera: int = PUNTOS_MINIMOS_FUERA) -> list[EventoSalida]:
    """clasificados: puntos consolidados y ordenados cronológicamente."""
    if not clasificados:
        return []
    tramos = calcular_tramos([c.punto for c in clasificados])
    n = len(clasificados)
    eventos = []

    def hay_hueco(desde: int, hasta: int) -> bool:
        return any(tramos[k].tipo == HUECO_CON_DESPLAZAMIENTO for k in range(desde, hasta))

    i = 0
    while i < n:
        if clasificados[i].estado == DENTRO:
            i += 1
            continue
        j = i  # periodo [i, j): puntos consecutivos no DENTRO
        while j < n and clasificados[j].estado != DENTRO:
            j += 1

        periodo = clasificados[i:j]
        indices_fuera = [k for k in range(i, j) if clasificados[k].estado == FUERA]
        if indices_fuera:  # un periodo solo de TOLERANCIA no es evento
            primero, ultimo = indices_fuera[0], indices_fuera[-1]
            puntos_fuera = [clasificados[k] for k in indices_fuera]
            nombres, claves = _nombres_entidades(puntos_fuera)

            observable = timedelta()
            huecos_internos = 0
            for k in range(primero, ultimo):
                if clasificados[k].estado == FUERA and clasificados[k + 1].estado == FUERA:
                    if tramos[k].tipo == HUECO_CON_DESPLAZAMIENTO:
                        huecos_internos += 1
                    else:
                        observable += tramos[k].duracion

            antes = clasificados[i - 1] if i > 0 else None
            despues = clasificados[j] if j < n else None
            maxima = (despues.punto.fecha - antes.punto.fecha) if antes and despues else None

            eventos.append(EventoSalida(
                confirmado=_racha_maxima_fuera(periodo) >= puntos_minimos_fuera,
                inicia_fuera=antes is None,
                abierta=despues is None,
                ultimo_punto_dentro=antes,
                primer_punto_fuera=clasificados[primero],
                ultimo_punto_fuera=clasificados[ultimo],
                primer_punto_dentro_posterior=despues,
                entidades=nombres,
                claves_entidades=claves,
                cantidad_puntos_fuera=len(indices_fuera),
                cantidad_puntos_tolerancia=sum(1 for p in periodo if p.estado == TOLERANCIA),
                puntos_periodo=tuple(periodo),
                salida_con_hueco=antes is not None and hay_hueco(i - 1, primero),
                regreso_con_hueco=despues is not None and hay_hueco(ultimo, j),
                huecos_durante_evento=huecos_internos,
                duracion_observable_fuera=observable,
                duracion_maxima_posible=maxima,
            ))
        i = j
    return eventos


def resumir_geofencing(puntos: list[Punto], zonas: Zonas,
                       puntos_minimos_fuera: int = PUNTOS_MINIMOS_FUERA) -> ResumenGeofencing:
    """puntos: ya consolidados y ordenados (ver metricas.consolidar_puntos)."""
    clasificados = clasificar_puntos(puntos, zonas)
    eventos = detectar_eventos(clasificados, puntos_minimos_fuera)

    entidades: dict[str, None] = {}
    for e in eventos:
        if e.confirmado:
            entidades.update(dict.fromkeys(e.entidades))

    return ResumenGeofencing(
        puntos_dentro=sum(1 for c in clasificados if c.estado == DENTRO),
        puntos_tolerancia=sum(1 for c in clasificados if c.estado == TOLERANCIA),
        puntos_fuera=sum(1 for c in clasificados if c.estado == FUERA),
        eventos=eventos,
        salidas_confirmadas=sum(1 for e in eventos if e.es_salida_confirmada),
        salidas_no_confirmadas=sum(1 for e in eventos if not e.confirmado),
        inicia_fuera=any(e.inicia_fuera and e.confirmado for e in eventos),
        termino_fuera=bool(clasificados) and clasificados[-1].estado == FUERA,
        entidades_fuera=tuple(entidades),
        clasificados=clasificados,
    )


def asociar_con_trayectos(eventos: list[EventoSalida], trayectos: list[Trayecto]) -> dict[int, list[EventoSalida]]:
    """{número de trayecto (desde 1) -> eventos confirmados que se solapan en el tiempo}.

    El periodo del evento va del primer al último punto observado FUERA.
    """
    asociados: dict[int, list[EventoSalida]] = {}
    for numero, trayecto in enumerate(trayectos, start=1):
        for e in eventos:
            if not e.confirmado:
                continue
            inicio, fin = e.primer_punto_fuera.punto.fecha, e.ultimo_punto_fuera.punto.fecha
            if inicio <= trayecto.fin and fin >= trayecto.inicio:
                asociados.setdefault(numero, []).append(e)
    return asociados
