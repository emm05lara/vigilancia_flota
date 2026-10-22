"""Prioridad operativa por vehículo, estado de datos, resumen de flota, filtros y CSV.

Consume los ResultadoVehiculo que produce analizador.py; no recalcula nada de GPS.
No depende de Streamlit ni de Folium.

Dos conceptos independientes:
- PRIORIDAD operativa del vehículo: CRITICA / ALTA / REVISAR / MOVIMIENTO_ALTO / NORMAL.
  Solo existe si hay datos analizables. Sin datos queda en None ("sin determinar"):
  un vehículo sin datos nunca es NORMAL ni tiene distancia 0.
- ESTADO DE DATOS: OK / PARCIAL / SIN_GPX / ERROR. Una carpeta sin GPX no es un problema
  del vehículo (quizá solo falta cargar el archivo): es un estado de datos.

El volumen de movimiento diario se describe (BAJO / NORMAL / ALTO / MUY ALTO); BAJO y NORMAL no
generan prioridad. No se llama "anomalía"
porque aún no hay histórico para saber qué es normal en cada vehículo. El nivel se evalúa
POR DÍA CALENDARIO (America/Mexico_City) con los mismos umbrales, y el nivel del periodo es
el máximo diario: así no depende de cuántos días contiene el GPX ni un promedio oculta un
día de mucha actividad.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta

from .analizador import ResultadoVehiculo
from .parametros import (DISTANCIA_MOVIMIENTO_ALTO_KM, DISTANCIA_MOVIMIENTO_BAJO_KM,
                         DISTANCIA_MOVIMIENTO_MUY_ALTO_KM)

# Prioridad operativa del vehículo, de mayor a menor atención.
CRITICA = "CRITICA"
ALTA = "ALTA"
REVISAR = "REVISAR"
MOVIMIENTO_ALTO = "MOVIMIENTO_ALTO"
NORMAL = "NORMAL"
ORDEN_PRIORIDADES = [CRITICA, ALTA, REVISAR, MOVIMIENTO_ALTO, NORMAL]
PRIORIDADES_DE_ATENCION = (CRITICA, ALTA, REVISAR, MOVIMIENTO_ALTO)

# Nivel de movimiento (según distancia estimada observada).
MOV_BAJO = "BAJO"  # solo descriptivo: no es un problema y no genera prioridad
MOV_NORMAL = "NORMAL"
MOV_ALTO = "ALTO"
MOV_MUY_ALTO = "MUY ALTO"
NIVELES_MOVIMIENTO = [MOV_BAJO, MOV_NORMAL, MOV_ALTO, MOV_MUY_ALTO]  # de menor a mayor

# Estado de los datos del vehículo.
DATOS_OK = "OK"
DATOS_PARCIAL = "PARCIAL"  # hay datos válidos, pero algún GPX falló
DATOS_SIN_GPX = "SIN_GPX"  # la carpeta no contiene .gpx
DATOS_ERROR = "ERROR"  # hay GPX pero ninguno aporta datos (todos fallaron o no tienen puntos)
# Estados que solo existen en el modo por PERIODO BAANOOL (se derivan del manifiesto de adquisición):
DATOS_SIN_DATOS_PERIODO = "SIN_DATOS_PERIODO"  # BAANOOL respondió, pero no entregó posiciones GPS
DATOS_SIN_DISPOSITIVO = "SIN_DISPOSITIVO_BAANOOL"  # la LLAVE local no tiene dispositivo BAANOOL exacto
DATOS_ERROR_ADQUISICION = "ERROR_ADQUISICION"  # la adquisición del periodo falló o no fue posible
# Estados derivados del análisis local de un subperiodo (no describen lo ocurrido en la adquisición):
DATOS_SIN_POSICIONES_SUBPERIODO = "SIN_POSICIONES_SUBPERIODO"  # el GPX existe, sin puntos en el intervalo
DATOS_COBERTURA_LOCAL_INSUFICIENTE = "COBERTURA_LOCAL_INSUFICIENTE"  # ninguna fuente local cubre el intervalo
ESTADOS_DATOS = [DATOS_OK, DATOS_PARCIAL, DATOS_SIN_DATOS_PERIODO, DATOS_SIN_POSICIONES_SUBPERIODO,
                 DATOS_SIN_DISPOSITIVO, DATOS_ERROR_ADQUISICION, DATOS_COBERTURA_LOCAL_INSUFICIENTE,
                 DATOS_SIN_GPX, DATOS_ERROR]
ESTADOS_SIN_DATOS = (DATOS_SIN_GPX, DATOS_ERROR, DATOS_SIN_DATOS_PERIODO, DATOS_SIN_DISPOSITIVO,
                     DATOS_ERROR_ADQUISICION, DATOS_SIN_POSICIONES_SUBPERIODO, DATOS_COBERTURA_LOCAL_INSUFICIENTE)
ESTADOS_PROBLEMA_DATOS = (DATOS_PARCIAL,) + ESTADOS_SIN_DATOS


@dataclass(frozen=True)
class EvaluacionVehiculo:
    resultado: ResultadoVehiculo  # el resultado completo, para el detalle
    identificador: str  # LLAVE original, o "[SIN LLAVE] carpeta"
    estado_datos: str
    prioridad: str | None  # None = sin determinar (no hay datos)
    motivos: tuple[str, ...]
    nivel_movimiento: str | None  # nivel del PERIODO = máximo nivel diario; None si no hay datos
    distancia_observada_km: float | None  # total del periodo; None si no hay datos (no es lo mismo que 0)
    distancia_por_dia: dict[date, float] | None  # distancia observada por día calendario
    nivel_por_dia: dict[date, str] | None
    distancia_maxima_diaria_km: float | None
    fecha_distancia_maxima: date | None
    trayectos: int | None
    salidas_confirmadas: int | None
    termino_fuera: bool | None  # periodo abierto CONFIRMADO fuera de la zona
    ultimo_punto_fuera: bool | None  # diagnóstico: el último punto geográfico estaba fuera, confirmado o no
    entidades_fuera: tuple[str, ...]
    huecos_con_desplazamiento: int | None
    conflictos_timestamp: int
    errores: tuple[tuple[str, str], ...]  # (archivo, error)
    # Calidad GPS: dimensión independiente (SIN_ALERTAS / REVISAR). Solo se muestra; NO influye
    # en la prioridad. None si no hay datos.
    calidad_gps: str | None = None
    # Adquisición (solo en modo por periodo, desde el manifiesto). Dimensiones independientes:
    # estado_adquisicion = qué pasó al pedir el periodo; estado_actual_baanool = ONLINE/OFFLINE ahora
    # (fotografía de la consulta; NO describe el periodo y NO altera ningún estado).
    estado_adquisicion: str | None = None
    estado_actual_baanool: str | None = None
    inconsistencia: str | None = None  # p. ej. el manifiesto referencia un archivo que ya no existe

    @property
    def tiene_datos(self) -> bool:
        return self.estado_datos not in ESTADOS_SIN_DATOS

    @property
    def requiere_atencion(self) -> bool:
        """Incidencia operativa del vehículo. Sin datos NO cuenta."""
        return self.prioridad in PRIORIDADES_DE_ATENCION

    @property
    def problema_de_datos(self) -> bool:
        return self.estado_datos in ESTADOS_PROBLEMA_DATOS


@dataclass(frozen=True)
class ResumenFlota:
    # Cobertura de datos (carpetas)
    carpetas_encontradas: int
    con_datos_validos: int  # estado OK
    parciales: int
    sin_gpx: int
    con_error: int  # estado ERROR
    # Análisis vehicular (solo vehículos con datos: OK + PARCIAL)
    analizados: int
    por_prioridad: dict[str, int]
    con_salida_confirmada: int
    terminaron_fuera: int
    movimiento_alto: int  # nivel ALTO o MUY ALTO
    con_huecos: int
    # Modo por periodo BAANOOL (0 en el modo local)
    sin_datos_periodo: int = 0
    sin_dispositivo: int = 0
    errores_adquisicion: int = 0
    sin_posiciones_subperiodo: int = 0
    cobertura_insuficiente: int = 0

    @property
    def sin_novedad(self) -> int:
        return self.por_prioridad[NORMAL]


def nivel_movimiento(distancia_km: float,
                     alto_km: float = DISTANCIA_MOVIMIENTO_ALTO_KM,
                     muy_alto_km: float = DISTANCIA_MOVIMIENTO_MUY_ALTO_KM,
                     bajo_km: float = DISTANCIA_MOVIMIENTO_BAJO_KM) -> str:
    """Nivel de UN día. Límites exactos: <bajo BAJO; [bajo, alto] NORMAL; (alto, muy_alto] ALTO; >muy_alto MUY ALTO."""
    if distancia_km > muy_alto_km:
        return MOV_MUY_ALTO
    if distancia_km > alto_km:
        return MOV_ALTO
    if distancia_km < bajo_km:
        return MOV_BAJO
    return MOV_NORMAL


def nivel_maximo(niveles) -> str:
    """El mayor nivel de movimiento (BAJO < NORMAL < ALTO < MUY ALTO)."""
    return max(niveles, key=NIVELES_MOVIMIENTO.index, default=MOV_BAJO)


def _duracion_corta(d: timedelta) -> str:
    minutos = round(d.total_seconds() / 60)
    horas, minutos = divmod(minutos, 60)
    if horas and minutos:
        return f"{horas} h {minutos} min"
    return f"{horas} h" if horas else f"{minutos} min"


def _fecha(f) -> str:
    return f.strftime("%Y-%m-%d %H:%M")


def _estado_datos(v: ResultadoVehiculo) -> str:
    if not v.gpx:
        return DATOS_SIN_GPX
    validos = [g for g in v.gpx if not g.error]
    if not validos or v.resumen is None or v.resumen.cantidad_puntos == 0:
        return DATOS_ERROR
    return DATOS_PARCIAL if len(validos) < len(v.gpx) else DATOS_OK


def _motivos_sin_datos(estado: str, errores) -> tuple[str, ...]:
    if estado == DATOS_SIN_GPX:
        return ("Carpeta sin archivos .gpx: sin datos (quizá falta cargar el archivo)",)
    if errores:
        return (f"Ningún GPX aportó datos: {len(errores)} archivo(s) con error",) \
            + tuple(f"{archivo}: {error}" for archivo, error in errores)
    return ("Los GPX no contienen puntos: sin datos utilizables",)


def evaluar_vehiculo(v: ResultadoVehiculo,
                     alto_km: float = DISTANCIA_MOVIMIENTO_ALTO_KM,
                     muy_alto_km: float = DISTANCIA_MOVIMIENTO_MUY_ALTO_KM,
                     bajo_km: float = DISTANCIA_MOVIMIENTO_BAJO_KM) -> EvaluacionVehiculo:
    identificador = v.llave_original or f"[SIN LLAVE] {v.nombre_carpeta}"
    errores = tuple((g.archivo.name, g.error) for g in v.gpx if g.error)
    estado = _estado_datos(v)

    if estado in ESTADOS_SIN_DATOS:  # prioridad sin determinar; ninguna métrica inventada
        return EvaluacionVehiculo(
            resultado=v, identificador=identificador, estado_datos=estado, prioridad=None,
            motivos=_motivos_sin_datos(estado, errores), nivel_movimiento=None,
            distancia_observada_km=None, distancia_por_dia=None, nivel_por_dia=None,
            distancia_maxima_diaria_km=None, fecha_distancia_maxima=None, trayectos=None, salidas_confirmadas=None, termino_fuera=None,
            ultimo_punto_fuera=None, entidades_fuera=(), huecos_con_desplazamiento=None,
            conflictos_timestamp=len(v.conflictos_timestamp), errores=errores)

    r, a, g = v.resumen, v.actividad, v.geofencing
    distancia = r.distancia_estimada_observada_km  # total del periodo (no cambia)
    por_dia = a.distancia_por_dia
    niveles_dia = {d: nivel_movimiento(km, alto_km, muy_alto_km, bajo_km) for d, km in por_dia.items()}
    nivel = nivel_maximo(niveles_dia.values())  # nivel del periodo = máximo nivel diario
    fecha_max = max(por_dia, key=por_dia.get) if por_dia else None  # la primera fecha si hay empate
    distancia_max = por_dia[fecha_max] if fecha_max else None

    # Incidencias que justifican REVISAR (se listan siempre, aunque otra prioridad gane).
    por_revisar: list[str] = []
    for h in r.huecos_con_desplazamiento:
        por_revisar.append(
            f"Hueco de información de {_duracion_corta(h.duracion)} con desplazamiento mínimo de "
            f"{h.distancia_km:.1f} km ({_fecha(h.inicio)} -> {_fecha(h.fin)}); trayectoria desconocida")
    if v.conflictos_timestamp:
        por_revisar.append(f"{len(v.conflictos_timestamp)} conflicto(s) de timestamp "
                           "(misma fecha/hora en lugares distintos)")
    if estado == DATOS_PARCIAL:
        por_revisar.append(f"Datos parciales: {len(errores)} archivo(s) GPX con error; "
                           "se analizaron los válidos")

    termino_fuera = False
    ultimo_fuera = None
    salidas = None
    entidades: tuple[str, ...] = ()
    if g is None:
        por_revisar.append("Geofencing no disponible (zonas no cargadas): no se verificó la zona permitida")
    else:
        salidas = g.salidas_confirmadas
        entidades = g.entidades_fuera
        ultimo_fuera = g.termino_fuera  # el último punto estaba FUERA (aunque no se confirme)
        # "Terminó fuera" operativo = periodo abierto CONFIRMADO. Un único punto fuera al final
        # no alcanza PUNTOS_MINIMOS_FUERA: queda en REVISAR, no en CRITICA.
        termino_fuera = any(e.abierta and e.confirmado for e in g.eventos)
        for e in g.eventos:
            if e.abierta and not e.confirmado:
                por_revisar.append("Último punto fuera de zona; salida no confirmada "
                                   "(no alcanza los puntos fuera mínimos)")
            if not e.confirmado:
                continue
            if e.inicia_fuera:
                por_revisar.append("El periodo analizado empieza fuera de la zona (la salida no fue observada)")
            if e.salida_con_hueco or e.regreso_con_hueco or e.huecos_durante_evento:
                por_revisar.append(
                    f"Salida a {', '.join(e.entidades)} con información incompleta: hueco con "
                    "desplazamiento durante el evento (hora de cruce y trayectoria desconocidas)")

    if termino_fuera:
        prioridad = CRITICA
        motivos = [f"Terminó fuera de la zona permitida ({', '.join(entidades) or 'sin entidad identificada'})"]
    elif salidas:
        prioridad = ALTA
        motivos = [f"{salidas} salida(s) confirmada(s) de la zona permitida; terminó nuevamente dentro "
                   f"({', '.join(entidades)})"]
    elif por_revisar:
        prioridad = REVISAR
        motivos = []
    elif nivel in (MOV_ALTO, MOV_MUY_ALTO):  # BAJO y NORMAL no generan MOVIMIENTO_ALTO
        prioridad = MOVIMIENTO_ALTO
        motivos = [f"Movimiento {nivel}: {distancia_max:.0f} km de distancia estimada observada el "
                   f"{fecha_max:%Y-%m-%d} (máximo diario; total del periodo {distancia:.0f} km)"]
    else:
        prioridad = NORMAL
        motivos = []

    return EvaluacionVehiculo(
        resultado=v, identificador=identificador, estado_datos=estado, prioridad=prioridad,
        motivos=tuple(motivos + por_revisar), nivel_movimiento=nivel, distancia_observada_km=distancia,
        distancia_por_dia=por_dia, nivel_por_dia=niveles_dia, distancia_maxima_diaria_km=distancia_max,
        fecha_distancia_maxima=fecha_max,
        trayectos=len(a.trayectos), salidas_confirmadas=salidas,
        termino_fuera=termino_fuera if g is not None else None, ultimo_punto_fuera=ultimo_fuera,
        entidades_fuera=entidades, huecos_con_desplazamiento=a.cantidad_huecos_con_desplazamiento,
        conflictos_timestamp=len(v.conflictos_timestamp), errores=errores,
        calidad_gps=v.calidad_gps.estado if v.calidad_gps else None)


def ordenar_evaluaciones(evaluaciones: list[EvaluacionVehiculo]) -> list[EvaluacionVehiculo]:
    """Por prioridad y, dentro de cada una, distancia descendente.

    Los vehículos sin prioridad (sin datos) van al final: su estado de datos es lo relevante.
    """
    def clave(e: EvaluacionVehiculo):
        indice = ORDEN_PRIORIDADES.index(e.prioridad) if e.prioridad else len(ORDEN_PRIORIDADES)
        distancia = e.distancia_observada_km if e.distancia_observada_km is not None else 0.0
        return indice, -distancia, e.estado_datos, e.identificador

    return sorted(evaluaciones, key=clave)


def evaluar_flota(resultados: list[ResultadoVehiculo],
                  alto_km: float = DISTANCIA_MOVIMIENTO_ALTO_KM,
                  muy_alto_km: float = DISTANCIA_MOVIMIENTO_MUY_ALTO_KM,
                  bajo_km: float = DISTANCIA_MOVIMIENTO_BAJO_KM) -> list[EvaluacionVehiculo]:
    return ordenar_evaluaciones([evaluar_vehiculo(v, alto_km, muy_alto_km, bajo_km) for v in resultados])


def resumir_flota(evaluaciones: list[EvaluacionVehiculo]) -> ResumenFlota:
    con_datos = [e for e in evaluaciones if e.tiene_datos]
    return ResumenFlota(
        carpetas_encontradas=len(evaluaciones),
        con_datos_validos=sum(1 for e in evaluaciones if e.estado_datos == DATOS_OK),
        parciales=sum(1 for e in evaluaciones if e.estado_datos == DATOS_PARCIAL),
        sin_gpx=sum(1 for e in evaluaciones if e.estado_datos == DATOS_SIN_GPX),
        con_error=sum(1 for e in evaluaciones if e.estado_datos == DATOS_ERROR),
        analizados=len(con_datos),
        por_prioridad={p: sum(1 for e in con_datos if e.prioridad == p) for p in ORDEN_PRIORIDADES},
        con_salida_confirmada=sum(1 for e in con_datos if e.salidas_confirmadas),
        terminaron_fuera=sum(1 for e in con_datos if e.termino_fuera),
        movimiento_alto=sum(1 for e in con_datos if e.nivel_movimiento in (MOV_ALTO, MOV_MUY_ALTO)),
        con_huecos=sum(1 for e in con_datos if e.huecos_con_desplazamiento),
        sin_datos_periodo=sum(1 for e in evaluaciones if e.estado_datos == DATOS_SIN_DATOS_PERIODO),
        sin_dispositivo=sum(1 for e in evaluaciones if e.estado_datos == DATOS_SIN_DISPOSITIVO),
        errores_adquisicion=sum(1 for e in evaluaciones if e.estado_datos == DATOS_ERROR_ADQUISICION),
        sin_posiciones_subperiodo=sum(1 for e in evaluaciones if e.estado_datos == DATOS_SIN_POSICIONES_SUBPERIODO),
        cobertura_insuficiente=sum(1 for e in evaluaciones if e.estado_datos == DATOS_COBERTURA_LOCAL_INSUFICIENTE),
    )


def filtrar_evaluaciones(evaluaciones: list[EvaluacionVehiculo], *,
                         prioridades: list[str] | None = None,
                         salida: str = "todos",  # "todos" | "con" | "sin"
                         termino_fuera: str = "todos",  # "todos" | "si" | "no"
                         entidades: list[str] | None = None,
                         niveles: list[str] | None = None,
                         solo_atencion: bool = False,
                         problemas_datos: bool = False,
                         estados_datos: list[str] | None = None,
                         texto: str | None = None) -> list[EvaluacionVehiculo]:
    """Un filtro vacío/None o "todos" no filtra. Conserva el orden recibido.

    solo_atencion: incidencias operativas del vehículo (CRITICA/ALTA/REVISAR/MOVIMIENTO_ALTO);
        NO incluye carpetas sin datos.
    problemas_datos: PARCIAL y todos los estados sin datos (SIN_GPX, ERROR, SIN_DATOS_PERIODO,
        SIN_DISPOSITIVO_BAANOOL, ERROR_ADQUISICION, SIN_POSICIONES_SUBPERIODO, COBERTURA_LOCAL_INSUFICIENTE).
    estados_datos: solo los vehículos cuyo estado de datos está en la lista.
    texto: búsqueda (sin distinguir mayúsculas) dentro de la LLAVE.
    Si ambos están activos se muestra la unión; si ninguno, no se filtra por esto.
    """
    resultado = []
    for e in evaluaciones:
        if (solo_atencion or problemas_datos) and not (
                (solo_atencion and e.requiere_atencion) or (problemas_datos and e.problema_de_datos)):
            continue
        if prioridades and e.prioridad not in prioridades:
            continue
        if estados_datos and e.estado_datos not in estados_datos:
            continue
        if texto and texto.strip().casefold() not in e.identificador.casefold():
            continue
        if salida == "con" and not e.salidas_confirmadas:
            continue
        if salida == "sin" and (not e.tiene_datos or e.salidas_confirmadas):
            continue
        if termino_fuera == "si" and not e.termino_fuera:
            continue
        if termino_fuera == "no" and (not e.tiene_datos or e.termino_fuera):
            continue
        if entidades and not set(entidades) & set(e.entidades_fuera):
            continue
        if niveles and e.nivel_movimiento not in niveles:
            continue
        resultado.append(e)
    return resultado


def _si_no(valor: bool | None) -> str:
    return "" if valor is None else ("SI" if valor else "NO")


COLUMNAS_CSV = [
    "LLAVE", "carpeta", "prioridad", "estado_datos", "estado_adquisicion", "estado_actual_baanool",
    "distancia_estimada_observada", "distancia_maxima_diaria", "fecha_distancia_maxima", "nivel_movimiento",
    "trayectos", "salidas_confirmadas", "termino_fuera", "ultimo_punto_fuera", "entidades_fuera",
    "huecos_con_desplazamiento", "conflictos_timestamp", "calidad_gps", "motivos",
]


def filas_csv(evaluaciones: list[EvaluacionVehiculo]) -> list[dict]:
    """Un dict por vehículo. Sin datos -> campos vacíos (no ceros) y prioridad vacía."""
    filas = []
    for e in evaluaciones:
        v = e.resultado
        filas.append({
            "LLAVE": v.llave_original or "",
            "carpeta": v.nombre_carpeta,
            "prioridad": e.prioridad or "",
            "estado_datos": e.estado_datos,
            "estado_adquisicion": e.estado_adquisicion or "",
            "estado_actual_baanool": e.estado_actual_baanool or "",
            "distancia_estimada_observada":
                "" if e.distancia_observada_km is None else f"{e.distancia_observada_km:.2f}",
            "distancia_maxima_diaria":
                "" if e.distancia_maxima_diaria_km is None else f"{e.distancia_maxima_diaria_km:.2f}",
            "fecha_distancia_maxima": e.fecha_distancia_maxima.isoformat() if e.fecha_distancia_maxima else "",
            "nivel_movimiento": e.nivel_movimiento or "",
            "trayectos": "" if e.trayectos is None else e.trayectos,
            "salidas_confirmadas": "" if e.salidas_confirmadas is None else e.salidas_confirmadas,
            "termino_fuera": _si_no(e.termino_fuera),
            "ultimo_punto_fuera": _si_no(e.ultimo_punto_fuera),
            "entidades_fuera": "; ".join(e.entidades_fuera),
            "huecos_con_desplazamiento":
                "" if e.huecos_con_desplazamiento is None else e.huecos_con_desplazamiento,
            "conflictos_timestamp": e.conflictos_timestamp,
            "calidad_gps": e.calidad_gps or "",
            "motivos": " | ".join(e.motivos),
        })
    return filas


def generar_csv(evaluaciones: list[EvaluacionVehiculo]) -> str:
    """CSV del resumen de flota, a partir de evaluaciones ya calculadas."""
    salida = io.StringIO()
    escritor = csv.DictWriter(salida, fieldnames=COLUMNAS_CSV, lineterminator="\n")
    escritor.writeheader()
    escritor.writerows(filas_csv(evaluaciones))
    return salida.getvalue()


def evaluacion_sin_datos(resultado: ResultadoVehiculo, estado_datos: str, motivos: tuple[str, ...],
                         estado_adquisicion: str | None = None, estado_actual_baanool: str | None = None,
                         inconsistencia: str | None = None) -> EvaluacionVehiculo:
    """Evaluación de un vehículo SIN posiciones analizables (modo por periodo).

    Nunca recibe prioridad ni métricas: sin datos no equivale a NORMAL ni a "sin movimiento". La calidad
    GPS tampoco se evalúa (no hay puntos). El estado actual ONLINE/OFFLINE solo se conserva como dato.
    """
    if estado_datos not in ESTADOS_SIN_DATOS:
        raise ValueError(f"{estado_datos!r} no es un estado sin datos")
    return EvaluacionVehiculo(
        resultado=resultado, identificador=resultado.llave_original or f"[SIN LLAVE] {resultado.nombre_carpeta}",
        estado_datos=estado_datos, prioridad=None, motivos=tuple(motivos), nivel_movimiento=None,
        distancia_observada_km=None, distancia_por_dia=None, nivel_por_dia=None,
        distancia_maxima_diaria_km=None, fecha_distancia_maxima=None, trayectos=None,
        salidas_confirmadas=None, termino_fuera=None, ultimo_punto_fuera=None, entidades_fuera=(),
        huecos_con_desplazamiento=None, conflictos_timestamp=0, errores=(), calidad_gps=None,
        estado_adquisicion=estado_adquisicion, estado_actual_baanool=estado_actual_baanool,
        inconsistencia=inconsistencia)
