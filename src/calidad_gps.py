"""Diagnóstico de calidad GPS: velocidades sospechosas y saltos de posicionamiento.

CAPA DE DIAGNÓSTICO. No modifica ni elimina puntos, no toca los GPX y no cambia ninguna
métrica oficial (distancia, movimiento diario, geofencing, prioridad). Los puntos que se
consideran espurios solo se excluyen de una versión DEPURADA EXPERIMENTAL, calculada aparte.

Dos niveles de diagnóstico:

- TRAMO_CON_VELOCIDAD_SOSPECHOSA: velocidad media estimada > VELOCIDAD_SOSPECHOSA_KMH.
  No significa que la posición sea errónea; la velocidad por sí sola nunca basta para
  excluir un punto.
- SALTO_GPS_PROBABLE: un único punto X entre A y B que se aleja bruscamente de la
  trayectoria y regresa casi de inmediato. Requiere evidencia conjunta:
    1. ventana corta: de A a B pasan <= VENTANA_MAXIMA_SALTO_SEGUNDOS;
    2. X queda a >= DISTANCIA_MINIMA_SALTO_KM de A y de B;
    3. velocidad extrema: A->X o X->B supera VELOCIDAD_SALTO_GPS_KMH;
    4. A->B es plausible por sí solo (velocidad A-B <= VELOCIDAD_SOSPECHOSA_KMH);
    5. (A->X + X->B) / (A->B) >= RELACION_DESVIO_MINIMA.
  Con intervalos largos (> ventana) nunca se clasifica. Un timestamp repetido (duración 0)
  dentro de la secuencia la hace no evaluable: es un timestamp anómalo, no un salto.

Casos no aislados (dos o más puntos consecutivos, candidatos que comparten puntos, o un
conflicto de timestamp en el entorno) NO se corrigen: se marcan ANOMALIA_GPS_COMPLEJA para
revisión manual.

Frecuencia de reporte: no se asume ninguna; todo se calcula con los timestamps reales.
"""

import math
from dataclasses import dataclass
from datetime import date

from .geocercas import ResumenGeofencing, resumir_geofencing
from .gpx_parser import Punto
from .metricas import TRAMO_NORMAL, ConflictoTimestamp, calcular_tramos, haversine_km
from .movimiento import distancia_por_dia
from .parametros import (DISTANCIA_MINIMA_SALTO_KM, RELACION_DESVIO_MINIMA, VELOCIDAD_SALTO_GPS_KMH,
                         VELOCIDAD_SOSPECHOSA_KMH, VENTANA_MAXIMA_SALTO_SEGUNDOS)
from .zonas import Zonas

CALIDAD_SIN_ALERTAS = "SIN_ALERTAS"
CALIDAD_REVISAR = "REVISAR"

TIPO_VELOCIDAD_SOSPECHOSA = "TRAMO_CON_VELOCIDAD_SOSPECHOSA"
TIPO_SALTO_PROBABLE = "SALTO_GPS_PROBABLE"
TIPO_ANOMALIA_COMPLEJA = "ANOMALIA_GPS_COMPLEJA"


@dataclass(frozen=True)
class TramoSospechosoGps:
    punto_inicial: Punto
    punto_final: Punto
    distancia_km: float
    duracion_segundos: float
    velocidad_estimada_kmh: float
    motivo: str
    relacionado_con: str | None  # TIPO_SALTO_PROBABLE / TIPO_ANOMALIA_COMPLEJA / None (solo velocidad)


@dataclass(frozen=True)
class EvaluacionSecuencia:
    """Medidas y condiciones de una secuencia A -> X1..Xk -> B (k=1: terna A-X-B)."""
    punto_anterior: Punto
    puntos_intermedios: tuple[Punto, ...]
    punto_siguiente: Punto
    indice_anterior: int
    indice_siguiente: int
    distancia_ax_km: float  # primer tramo (entrada)
    distancia_xb_km: float  # último tramo (regreso)
    distancia_ab_km: float
    distancia_recorrido_km: float  # A -> X1 -> ... -> Xk -> B
    duracion_ax_segundos: float
    duracion_xb_segundos: float
    duracion_total_segundos: float
    velocidad_ax_kmh: float | None  # None si la duración es 0
    velocidad_xb_kmh: float | None
    velocidad_ab_kmh: float | None
    relacion_desvio: float  # recorrido / A-B; inf si A y B coinciden y hubo recorrido
    condiciones: dict[str, bool]

    @property
    def cantidad_intermedios(self) -> int:
        return len(self.puntos_intermedios)

    @property
    def cumple(self) -> bool:
        return all(self.condiciones.values())


@dataclass(frozen=True)
class SaltoGpsProbable:
    punto_anterior: Punto  # A
    punto_sospechoso: Punto  # X (coordenada original, intacta)
    punto_siguiente: Punto  # B
    distancia_ax_km: float
    distancia_xb_km: float
    distancia_ab_km: float
    duracion_ax_segundos: float
    duracion_xb_segundos: float
    velocidad_ax_kmh: float
    velocidad_xb_kmh: float
    velocidad_ab_kmh: float
    relacion_desvio: float
    motivos: tuple[str, ...]

    @property
    def distancia_atribuida_km(self) -> float:
        """Lo que se descuenta experimentalmente: A-X + X-B - A-B."""
        return self.distancia_ax_km + self.distancia_xb_km - self.distancia_ab_km


@dataclass(frozen=True)
class AnomaliaGpsCompleja:
    punto_anterior: Punto
    puntos_sospechosos: tuple[Punto, ...]
    punto_siguiente: Punto
    distancia_recorrido_km: float
    distancia_ab_km: float
    duracion_total_segundos: float
    relacion_desvio: float
    motivos: tuple[str, ...]


@dataclass(frozen=True)
class ResumenCalidadGps:
    estado: str  # CALIDAD_SIN_ALERTAS | CALIDAD_REVISAR
    velocidad_maxima_kmh: float | None
    tramos_velocidad_sospechosa: list[TramoSospechosoGps]
    saltos_probables: list[SaltoGpsProbable]
    anomalias_complejas: list[AnomaliaGpsCompleja]
    # Distancias (solo tramos normales, como la oficial). La oficial NO se modifica.
    distancia_oficial_km: float
    distancia_depurada_km: float  # EXPERIMENTAL
    distancia_atribuida_saltos_km: float
    diferencia_porcentual: float
    distancia_por_dia_depurada: dict[date, float]  # EXPERIMENTAL
    puntos_excluidos_experimental: tuple[Punto, ...]  # los X de saltos probables
    geofencing_depurado: ResumenGeofencing | None  # EXPERIMENTAL; None si no hay saltos o zonas

    @property
    def hay_alertas(self) -> bool:
        return self.estado == CALIDAD_REVISAR


# ---------- medidas ----------

def _segundos(a: Punto, b: Punto) -> float:
    return b.fecha.timestamp() - a.fecha.timestamp()


def _distancia(a: Punto, b: Punto) -> float:
    return haversine_km(a.latitud, a.longitud, b.latitud, b.longitud)


def _velocidad(distancia_km: float, segundos: float) -> float | None:
    """km/h, o None si no hay duración (nunca una velocidad infinita)."""
    return distancia_km / (segundos / 3600) if segundos > 0 else None


def _relacion(recorrido_km: float, ab_km: float) -> float:
    if ab_km > 0:
        return recorrido_km / ab_km
    return math.inf if recorrido_km > 0 else 0.0  # A y B coinciden: sin división por cero


def evaluar_secuencia(puntos: list[Punto], a: int, b: int) -> EvaluacionSecuencia:
    """Evalúa la secuencia entre los índices a y b (b >= a + 2). Función pura, para diagnóstico."""
    A, B = puntos[a], puntos[b]
    intermedios = puntos[a + 1:b]
    primero, ultimo = intermedios[0], intermedios[-1]

    ax, xb, ab = _distancia(A, primero), _distancia(ultimo, B), _distancia(A, B)
    recorrido = sum(_distancia(puntos[i], puntos[i + 1]) for i in range(a, b))
    dur_ax, dur_xb, total = _segundos(A, primero), _segundos(ultimo, B), _segundos(A, B)
    v_ax, v_xb, v_ab = _velocidad(ax, dur_ax), _velocidad(xb, dur_xb), _velocidad(ab, total)
    relacion = _relacion(recorrido, ab)

    condiciones = {
        "tiempos_validos": all(_segundos(puntos[i], puntos[i + 1]) > 0 for i in range(a, b)),
        "ventana_corta": 0 < total <= VENTANA_MAXIMA_SALTO_SEGUNDOS,
        "puntos_desplazados": all(
            min(_distancia(A, x), _distancia(x, B)) >= DISTANCIA_MINIMA_SALTO_KM for x in intermedios),
        "velocidad_extrema": any(v is not None and v > VELOCIDAD_SALTO_GPS_KMH for v in (v_ax, v_xb)),
        "a_b_coherente": v_ab is not None and v_ab <= VELOCIDAD_SOSPECHOSA_KMH,
        "relacion_desvio": relacion >= RELACION_DESVIO_MINIMA,
    }
    return EvaluacionSecuencia(
        punto_anterior=A, puntos_intermedios=tuple(intermedios), punto_siguiente=B,
        indice_anterior=a, indice_siguiente=b,
        distancia_ax_km=ax, distancia_xb_km=xb, distancia_ab_km=ab, distancia_recorrido_km=recorrido,
        duracion_ax_segundos=dur_ax, duracion_xb_segundos=dur_xb, duracion_total_segundos=total,
        velocidad_ax_kmh=v_ax, velocidad_xb_kmh=v_xb, velocidad_ab_kmh=v_ab,
        relacion_desvio=relacion, condiciones=condiciones)


def _motivos(e: EvaluacionSecuencia) -> tuple[str, ...]:
    def v(x):
        return "sin duración" if x is None else f"{x:.0f} km/h"

    relacion = "∞ (A y B coinciden)" if math.isinf(e.relacion_desvio) else f"{e.relacion_desvio:.1f}"
    return (
        f"Ventana corta: {e.duracion_total_segundos:.0f} s de A a B (máx. {VENTANA_MAXIMA_SALTO_SEGUNDOS} s)",
        f"Punto(s) desplazado(s) >= {DISTANCIA_MINIMA_SALTO_KM:.2f} km de A y de B "
        f"(A-X {e.distancia_ax_km:.2f} km, X-B {e.distancia_xb_km:.2f} km)",
        f"Velocidad extrema: A-X {v(e.velocidad_ax_kmh)}, X-B {v(e.velocidad_xb_kmh)} "
        f"(umbral {VELOCIDAD_SALTO_GPS_KMH} km/h)",
        f"A-B coherente por sí solo: {e.distancia_ab_km:.2f} km a {v(e.velocidad_ab_kmh)}",
        f"Relación de desvío {relacion} (mín. {RELACION_DESVIO_MINIMA})",
    )


# ---------- detección ----------

def _buscar_candidatos(puntos: list[Punto]) -> list[EvaluacionSecuencia]:
    """Para cada A, la secuencia más corta A -> X1..Xk -> B que cumple todas las condiciones."""
    n = len(puntos)
    if n < 3:
        return []
    t = [p.fecha.timestamp() for p in puntos]
    v_tramo = [_velocidad(_distancia(puntos[i], puntos[i + 1]), t[i + 1] - t[i]) for i in range(n - 1)]
    extrema = [v is not None and v > VELOCIDAD_SALTO_GPS_KMH for v in v_tramo]

    candidatos = []
    for a in range(n - 2):
        b = a + 2
        while b < n and t[b] - t[a] <= VENTANA_MAXIMA_SALTO_SEGUNDOS:
            if extrema[a] or extrema[b - 1]:  # filtro barato antes de evaluar la secuencia completa
                evaluacion = evaluar_secuencia(puntos, a, b)
                if evaluacion.cumple:
                    candidatos.append(evaluacion)
                    break
            b += 1
    return candidatos


def _agrupar_solapados(candidatos: list[EvaluacionSecuencia]) -> list[list[EvaluacionSecuencia]]:
    """Agrupa candidatos que comparten algún punto (rangos de índices que se intersectan)."""
    grupos: list[list[EvaluacionSecuencia]] = []
    fin_grupo = -1
    for c in sorted(candidatos, key=lambda c: c.indice_anterior):
        if grupos and c.indice_anterior <= fin_grupo:
            grupos[-1].append(c)
            fin_grupo = max(fin_grupo, c.indice_siguiente)
        else:
            grupos.append([c])
            fin_grupo = c.indice_siguiente
    return grupos


def _clasificar(puntos: list[Punto], conflictos) -> tuple[list[SaltoGpsProbable], list[AnomaliaGpsCompleja],
                                                          set[int], list[tuple[int, int]]]:
    candidatos = _buscar_candidatos(puntos)

    # Un candidato con varios puntos que contiene el X de uno aislado queda explicado por este último.
    xs_aislados = {c.indice_anterior + 1 for c in candidatos if c.cantidad_intermedios == 1}
    candidatos = [c for c in candidatos if c.cantidad_intermedios == 1 or not any(
        c.indice_anterior < x < c.indice_siguiente for x in xs_aislados)]

    saltos: list[SaltoGpsProbable] = []
    anomalias: list[AnomaliaGpsCompleja] = []
    indices_salto: set[int] = set()  # índices de los X de saltos probables
    rangos_anomalia: list[tuple[int, int]] = []
    marcas_conflicto = [c.fecha.timestamp() for c in conflictos]

    for grupo in _agrupar_solapados(candidatos):
        desde = min(c.indice_anterior for c in grupo)
        hasta = max(c.indice_siguiente for c in grupo)
        conflicto_cerca = any(puntos[desde].fecha.timestamp() <= m <= puntos[hasta].fecha.timestamp()
                              for m in marcas_conflicto)
        aislado = len(grupo) == 1 and grupo[0].cantidad_intermedios == 1 and not conflicto_cerca

        if aislado:
            e = grupo[0]
            saltos.append(SaltoGpsProbable(
                punto_anterior=e.punto_anterior, punto_sospechoso=e.puntos_intermedios[0],
                punto_siguiente=e.punto_siguiente, distancia_ax_km=e.distancia_ax_km,
                distancia_xb_km=e.distancia_xb_km, distancia_ab_km=e.distancia_ab_km,
                duracion_ax_segundos=e.duracion_ax_segundos, duracion_xb_segundos=e.duracion_xb_segundos,
                velocidad_ax_kmh=e.velocidad_ax_kmh, velocidad_xb_kmh=e.velocidad_xb_kmh,
                velocidad_ab_kmh=e.velocidad_ab_kmh, relacion_desvio=e.relacion_desvio,
                motivos=_motivos(e)))
            indices_salto.add(e.indice_anterior + 1)
            continue

        motivos = []
        if any(c.cantidad_intermedios > 1 for c in grupo):
            motivos.append("Dos o más puntos consecutivos fuera de la trayectoria")
        if len(grupo) > 1:
            motivos.append("Varios candidatos comparten puntos: la corrección no es un caso aislado")
        if conflicto_cerca:
            motivos.append("Conflicto de timestamp en el entorno")
        motivos.append("No se corrige automáticamente; revisión manual")
        sospechosos = tuple(puntos[i] for i in range(desde + 1, hasta)
                            if any(c.indice_anterior < i < c.indice_siguiente for c in grupo))
        recorrido = sum(_distancia(puntos[i], puntos[i + 1]) for i in range(desde, hasta))
        ab = _distancia(puntos[desde], puntos[hasta])
        anomalias.append(AnomaliaGpsCompleja(
            punto_anterior=puntos[desde], puntos_sospechosos=sospechosos, punto_siguiente=puntos[hasta],
            distancia_recorrido_km=recorrido, distancia_ab_km=ab,
            duracion_total_segundos=_segundos(puntos[desde], puntos[hasta]),
            relacion_desvio=_relacion(recorrido, ab), motivos=tuple(motivos)))
        rangos_anomalia.append((desde, hasta))

    return saltos, anomalias, indices_salto, rangos_anomalia


def analizar_calidad_gps(puntos: list[Punto], conflictos: list[ConflictoTimestamp] | tuple = (),
                         zonas: Zonas | None = None) -> ResumenCalidadGps:
    """puntos: consolidados y ordenados cronológicamente (ver metricas.consolidar_puntos).

    conflictos: conflictos de timestamp ya detectados al consolidar (opcional).
    zonas: si se pasa y hay saltos probables, se calcula un geofencing EXPERIMENTAL sin ellos.
    """
    tramos = calcular_tramos(puntos)
    saltos, anomalias, indices_salto, rangos_anomalia = _clasificar(puntos, conflictos)

    sospechosos = []
    for i, t in enumerate(tramos):
        v = t.velocidad_media_kmh
        if v is None or v <= VELOCIDAD_SOSPECHOSA_KMH:
            continue
        if i in indices_salto or i + 1 in indices_salto:  # A->X (tramo i) o X->B (tramo i+1)
            relacionado = TIPO_SALTO_PROBABLE
        elif any(desde <= i < hasta for desde, hasta in rangos_anomalia):
            relacionado = TIPO_ANOMALIA_COMPLEJA
        else:
            relacionado = None
        sospechosos.append(TramoSospechosoGps(
            punto_inicial=puntos[i], punto_final=puntos[i + 1], distancia_km=t.distancia_km,
            duracion_segundos=t.duracion.total_seconds(), velocidad_estimada_kmh=v,
            motivo=f"Velocidad media estimada {v:.0f} km/h > {VELOCIDAD_SOSPECHOSA_KMH} km/h "
                   f"(solo diagnóstico: no implica que la posición sea errónea)",
            relacionado_con=relacionado))

    velocidades = [t.velocidad_media_kmh for t in tramos if t.velocidad_media_kmh is not None]
    oficial = sum(t.distancia_km for t in tramos if t.tipo == TRAMO_NORMAL)

    # Versión depurada EXPERIMENTAL: se excluye únicamente el X de cada salto aislado.
    # distancia_depurada = oficial - AX - XB + AB, sin doble conteo (los saltos no comparten puntos).
    atribuida = sum(s.distancia_atribuida_km for s in saltos)
    depurada = oficial - atribuida
    excluidos = tuple(s.punto_sospechoso for s in saltos)
    if excluidos:
        puntos_depurados = [p for i, p in enumerate(puntos) if i not in indices_salto]
        por_dia = distancia_por_dia(puntos_depurados, calcular_tramos(puntos_depurados))
        geofencing = resumir_geofencing(puntos_depurados, zonas) if zonas else None
    else:
        por_dia = distancia_por_dia(puntos, tramos)
        geofencing = None

    hay_alertas = bool(sospechosos or saltos or anomalias)
    return ResumenCalidadGps(
        estado=CALIDAD_REVISAR if hay_alertas else CALIDAD_SIN_ALERTAS,
        velocidad_maxima_kmh=max(velocidades) if velocidades else None,
        tramos_velocidad_sospechosa=sospechosos, saltos_probables=saltos, anomalias_complejas=anomalias,
        distancia_oficial_km=oficial, distancia_depurada_km=depurada,
        distancia_atribuida_saltos_km=atribuida,
        diferencia_porcentual=(100 * atribuida / oficial) if oficial > 0 else 0.0,
        distancia_por_dia_depurada=por_dia, puntos_excluidos_experimental=excluidos,
        geofencing_depurado=geofencing)
