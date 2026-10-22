"""Análisis LOCAL de un subperiodo a partir de GPX adquiridos previamente (etapa 8). Sin Streamlit, sin red.

Separa dos conceptos:
- PERIODO DE ADQUISICIÓN: el intervalo que se pidió a BAANOOL (lo que declara cada manifiesto).
- PERIODO DE ANÁLISIS: el intervalo que se quiere estudiar; puede estar contenido en uno o más
  periodos de adquisición.

Política de selección (conservadora, por LLAVE):
1. Se consideran solo los manifiestos de salida/estado_descargas/ cuyo periodo REAL (leído del JSON, no del
   nombre del archivo) contiene COMPLETAMENTE el periodo de análisis.
2. Orden determinista de preferencia: adquisición exacta primero; luego la de menor duración; a igualdad,
   la ejecución más reciente; a igualdad, el nombre del manifiesto en orden alfabético.
3. Se usa el primer candidato con un GPX utilizable (estado DESCARGADO/YA_EXISTE, ruta relativa segura,
   archivo existente, carpeta que corresponde a la LLAVE). Un candidato preferido sin GPX utilizable no
   bloquea a uno posterior; la discrepancia queda en las advertencias.
4. NUNCA se combinan GPX de distintas adquisiciones: cada LLAVE se analiza con UN solo archivo, así que
   no hay puntos ni distancias duplicados. No se une el cubrimiento parcial de varios archivos.

Los GPX y los manifiestos no se modifican. Si la fuente es una adquisición mayor, los puntos se filtran
en memoria (inicio <= fecha <= fin) y el analizador existente calcula todo sobre ese conjunto. Una
adquisición exacta se analiza completa, exactamente como antes.

"Archivo que cubre el intervalo" (adquisición) y "posiciones realmente disponibles" son cosas distintas:
una LLAVE cuyo GPX no tiene puntos dentro del intervalo queda SIN_POSICIONES_SUBPERIODO (sin prioridad,
sin métricas); no se interpreta como vehículo detenido, GPS averiado ni pérdida de señal.
Los límites no se interpolan: la distancia observada puede ser menor que la real si faltan puntos
cercanos al inicio o al fin del intervalo o hay huecos de reporte.
"""

import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import MutableMapping

from crear_carpetas_llaves import limpiar_nombre_carpeta

from .analizador import ResultadoVehiculo, analizar_archivos
from .descarga_lote import nombre_archivo_lote
from .gpx_parser import ZONA_HORARIA
from .integracion_periodo import (ANALIZABLE, _clasificar, periodo_key)
from .llaves import cargar_llaves
from .manifiesto import CARPETA_MANIFIESTOS, VERSION_FORMATO, estados_por_llave, nombre_manifiesto
from .parametros import ZONA_HORARIA_LOCAL
from .prioridades import (DATOS_COBERTURA_LOCAL_INSUFICIENTE, DATOS_SIN_DATOS_PERIODO, DATOS_SIN_GPX,
                          DATOS_SIN_POSICIONES_SUBPERIODO, EvaluacionVehiculo, evaluacion_sin_datos,
                          evaluar_vehiculo, ordenar_evaluaciones)
from .zonas import Zonas

FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"
TIPO_EXACTA = "EXACTA"
TIPO_MAYOR = "ADQUISICION_MAYOR"

MENSAJE_SIN_POSICIONES = ("No se encontraron posiciones GPS dentro del intervalo seleccionado en los "
                          "archivos locales disponibles.")
NOTA_SIN_CONSULTA = "No se realizaron solicitudes a BAANOOL."


def texto_periodo(inicio: datetime, fin: datetime) -> str:
    return f"{inicio:%d/%m/%Y %H:%M:%S} → {fin:%d/%m/%Y %H:%M:%S}"


# ---------- manifiestos disponibles ----------

@dataclass(frozen=True, eq=False)
class ManifiestoLocal:
    """Un manifiesto de adquisición ya validado. Las fechas son las declaradas DENTRO del JSON."""
    nombre: str
    ruta: str  # relativa al proyecto
    inicio: datetime
    fin: datetime
    fecha_ejecucion: str | None
    lote_completo: bool | None
    entradas: dict  # llave -> entrada del manifiesto

    def cubre(self, inicio: datetime, fin: datetime) -> bool:
        return self.inicio <= inicio and fin <= self.fin

    def es_exacto(self, inicio: datetime, fin: datetime) -> bool:
        return self.inicio == inicio and self.fin == fin

    @property
    def duracion(self) -> timedelta:
        return self.fin - self.inicio

    @property
    def marca_ejecucion(self) -> float:
        try:
            return datetime.fromisoformat(self.fecha_ejecucion).timestamp()
        except (TypeError, ValueError):
            return 0.0

    def clave_orden(self, inicio: datetime, fin: datetime) -> tuple:
        """Menor = más preferido: exacta, luego menor duración, luego ejecución más reciente, luego nombre."""
        return (not self.es_exacto(inicio, fin), self.duracion, -self.marca_ejecucion, self.nombre)


def _leer_manifiesto(ruta: Path, raiz: Path) -> ManifiestoLocal:
    """Lanza ValueError con un mensaje legible si el archivo no es un manifiesto utilizable."""
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"no es un JSON legible ({type(exc).__name__})") from None
    if not isinstance(datos, dict) or datos.get("version_formato") != VERSION_FORMATO:
        raise ValueError("versión o formato no reconocidos")
    vehiculos = datos.get("vehiculos")
    if not isinstance(vehiculos, list) or not all(isinstance(v, dict) and isinstance(v.get("llave"), str)
                                                  for v in vehiculos):
        raise ValueError("lista de vehículos inválida")
    try:
        inicio = datetime.strptime(datos["periodo_inicio_local"], FORMATO_FECHA)
        fin = datetime.strptime(datos["periodo_fin_local"], FORMATO_FECHA)
    except (KeyError, TypeError, ValueError):
        raise ValueError("el periodo declarado no es válido") from None
    if inicio >= fin:
        raise ValueError("el periodo declarado tiene inicio >= fin")
    zona = datos.get("zona_horaria_local")
    if zona is not None and zona != ZONA_HORARIA_LOCAL:
        raise ValueError(f"zona horaria no soportada ({zona})")
    llaves = [v["llave"] for v in vehiculos]
    if len(set(llaves)) != len(llaves):
        raise ValueError("contiene LLAVEs repetidas")
    try:
        relativa = ruta.resolve().relative_to(raiz.resolve()).as_posix()
    except ValueError:
        relativa = f"{ruta.parent.name}/{ruta.name}"
    return ManifiestoLocal(nombre=ruta.name, ruta=relativa, inicio=inicio, fin=fin,
                           fecha_ejecucion=datos.get("fecha_ejecucion"), lote_completo=datos.get("lote_completo"),
                           entradas=estados_por_llave(datos))


def descubrir_manifiestos(raiz: Path, carpeta: Path | None = None
                          ) -> tuple[list[ManifiestoLocal], list[str]]:
    """Lee los *.json de la carpeta de manifiestos (solo lectura). Devuelve (válidos, avisos).

    Un archivo no válido se omite con un aviso; nunca se adivina su contenido. Si el nombre no coincide con
    el periodo declarado se avisa y se usa el periodo del JSON.
    """
    carpeta = carpeta or CARPETA_MANIFIESTOS
    validos: list[ManifiestoLocal] = []
    avisos: list[str] = []
    if not carpeta.is_dir():
        return validos, avisos
    for ruta in sorted(carpeta.glob("*.json")):
        try:
            m = _leer_manifiesto(ruta, raiz)
        except ValueError as exc:
            avisos.append(f"Manifiesto {ruta.name} omitido: {exc}")
            continue
        if ruta.name != nombre_manifiesto(m.inicio, m.fin):
            avisos.append(f"El nombre de {ruta.name} no coincide con su periodo declarado "
                          f"({texto_periodo(m.inicio, m.fin)}); se usa el periodo del contenido.")
        validos.append(m)
    return validos, avisos


# ---------- selección por LLAVE ----------

@dataclass(frozen=True)
class FuenteVehiculo:
    llave: str
    nombre_carpeta: str
    estado_datos: str  # ANALIZABLE o un estado sin datos
    estado_adquisicion: str | None
    estado_actual_baanool: str | None
    archivo: Path | None = None  # solo si ANALIZABLE
    tipo: str | None = None  # EXACTA | ADQUISICION_MAYOR (el manifiesto de origen), si lo hubo
    manifiesto: str | None = None  # ruta relativa del manifiesto de origen
    adquisicion_inicio: datetime | None = None
    adquisicion_fin: datetime | None = None
    mensaje: str | None = None
    inconsistencia: str | None = None
    advertencias: tuple[str, ...] = ()

    @property
    def analizable(self) -> bool:
        return self.estado_datos == ANALIZABLE

    @property
    def exacta(self) -> bool:
        return self.tipo == TIPO_EXACTA

    @property
    def archivo_relativo(self) -> str | None:
        return f"{self.archivo.parent.name}/{self.archivo.name}" if self.archivo else None


def _clasificar_entrada(entrada: dict, llave: str, m: ManifiestoLocal, raiz: Path):
    """_clasificar existente + comprobación de que el GPX está en la carpeta de ESA LLAVE."""
    estado, archivo, mensaje, inconsistencia = _clasificar(entrada, raiz, nombre_archivo_lote(m.inicio, m.fin))
    if estado == ANALIZABLE and archivo.parent.name.casefold() != limpiar_nombre_carpeta(llave).casefold():
        problema = "El archivo del manifiesto no está en la carpeta de esta LLAVE (posible mezcla de vehículos)"
        return DATOS_SIN_GPX, None, problema, problema
    return estado, archivo, mensaje, inconsistencia


def seleccionar_fuente(llave: str, inicio: datetime, fin: datetime, manifiestos: list[ManifiestoLocal],
                       raiz: Path) -> FuenteVehiculo:
    """Elige la ÚNICA fuente local de la LLAVE (o el motivo por el que no hay), sin leer ningún GPX."""
    carpeta = limpiar_nombre_carpeta(llave)
    cubren = sorted((m for m in manifiestos if m.cubre(inicio, fin)), key=lambda m: m.clave_orden(inicio, fin))

    def descripcion(m):
        return f"{texto_periodo(m.inicio, m.fin)} ({m.nombre})"

    omitidas: list[str] = []
    sin_gpx = []  # (manifiesto, estado, mensaje, inconsistencia, entrada)
    elegida = None
    equivalentes = []
    for m in cubren:
        entrada = m.entradas.get(llave)
        if entrada is None:
            continue
        estado, archivo, mensaje, inconsistencia = _clasificar_entrada(entrada, llave, m, raiz)
        if estado != ANALIZABLE:
            sin_gpx.append((m, estado, mensaje, inconsistencia, entrada))
            continue
        if elegida is None:
            elegida = (m, archivo, mensaje, entrada)
            # Los candidatos sin GPX utilizable que iban antes se informan como discrepancia.
            omitidas = [f"No se usó la adquisición {descripcion(o[0])} para esta LLAVE: {o[2]}" for o in sin_gpx]
        elif m.clave_orden(inicio, fin)[:2] == elegida[0].clave_orden(inicio, fin)[:2]:
            equivalentes.append(m)

    if elegida is not None:
        m, archivo, mensaje, entrada = elegida
        advertencias = list(omitidas)
        if equivalentes:
            advertencias.append(
                f"Hay {len(equivalentes) + 1} adquisiciones equivalentes que cubren el periodo; se eligió la "
                f"ejecución más reciente ({m.nombre}) y no se combinaron.")
        return FuenteVehiculo(
            llave=llave, nombre_carpeta=carpeta, estado_datos=ANALIZABLE,
            estado_adquisicion=entrada.get("estado_adquisicion"),
            estado_actual_baanool=entrada.get("estado_actual_baanool"), archivo=archivo,
            tipo=TIPO_EXACTA if m.es_exacto(inicio, fin) else TIPO_MAYOR, manifiesto=m.ruta,
            adquisicion_inicio=m.inicio, adquisicion_fin=m.fin, mensaje=mensaje,
            advertencias=tuple(advertencias))

    if sin_gpx:
        m, estado, mensaje, inconsistencia, entrada = sin_gpx[0]
        exacta = m.es_exacto(inicio, fin)
        if estado == DATOS_SIN_DATOS_PERIODO and not exacta:
            # La observación es sobre la adquisición mayor; BAANOOL no recibió una consulta de este subperiodo.
            estado = DATOS_SIN_POSICIONES_SUBPERIODO
            mensaje = (f"{MENSAJE_SIN_POSICIONES} La adquisición {descripcion(m)} no obtuvo posiciones para "
                       "este dispositivo; no se consultó BAANOOL para este subperiodo.")
        return FuenteVehiculo(
            llave=llave, nombre_carpeta=carpeta, estado_datos=estado,
            estado_adquisicion=entrada.get("estado_adquisicion"),
            estado_actual_baanool=entrada.get("estado_actual_baanool"),
            tipo=TIPO_EXACTA if exacta else TIPO_MAYOR, manifiesto=m.ruta, adquisicion_inicio=m.inicio,
            adquisicion_fin=m.fin, mensaje=mensaje, inconsistencia=inconsistencia)

    motivo = ("Ninguna adquisición local cubre completamente el periodo solicitado." if not cubren else
              "Las adquisiciones locales que cubren el periodo no incluyen esta LLAVE.")
    return FuenteVehiculo(llave=llave, nombre_carpeta=carpeta, estado_datos=DATOS_COBERTURA_LOCAL_INSUFICIENTE,
                          estado_adquisicion=None, estado_actual_baanool=None, mensaje=motivo)


# ---------- análisis ----------

def _evaluar_fuente(f: FuenteVehiculo, inicio: datetime, fin: datetime, raiz: Path,
                    zonas: Zonas | None) -> EvaluacionVehiculo:
    if not f.analizable:
        v = ResultadoVehiculo(carpeta=raiz / "entrada" / f.nombre_carpeta, nombre_carpeta=f.nombre_carpeta,
                              llave_original=f.llave)
        return evaluacion_sin_datos(v, f.estado_datos, (f.mensaje,) if f.mensaje else (),
                                    estado_adquisicion=f.estado_adquisicion,
                                    estado_actual_baanool=f.estado_actual_baanool, inconsistencia=f.inconsistencia)

    ventana = None if f.exacta else (inicio, fin)  # la adquisición exacta se analiza completa, como antes
    v = analizar_archivos(f.archivo.parent, [f.archivo], f.llave, zonas, ventana)
    if ventana is not None and not v.puntos and not any(g.error for g in v.gpx):
        fuera = sum(g.puntos_fuera_ventana for g in v.gpx)
        motivo = MENSAJE_SIN_POSICIONES + (f" El archivo tiene {fuera} punto(s) fuera del intervalo."
                                           if fuera else "")
        return evaluacion_sin_datos(v, DATOS_SIN_POSICIONES_SUBPERIODO, (motivo,),
                                    estado_adquisicion=f.estado_adquisicion,
                                    estado_actual_baanool=f.estado_actual_baanool)
    return replace(evaluar_vehiculo(v), estado_adquisicion=f.estado_adquisicion,
                   estado_actual_baanool=f.estado_actual_baanool)


@dataclass
class ResultadoAnalisisLocal:
    evaluaciones: list[EvaluacionVehiculo]
    clave_periodo: str
    inicio: datetime
    fin: datetime
    fuentes: dict[str, FuenteVehiculo]  # por LLAVE: procedencia de cada resultado
    sin_cobertura: bool  # ningún manifiesto contiene el periodo completo
    disponibles: list[ManifiestoLocal]  # todos los manifiestos válidos (para explicar la cobertura)
    avisos: list[str] = field(default_factory=list)  # manifiestos omitidos, nombres incoherentes
    llaves_fuera_del_catalogo: list[str] = field(default_factory=list)
    solicitudes_baanool: int = 0  # siempre 0: este análisis nunca consulta BAANOOL

    @property
    def usa_adquisicion_mayor(self) -> bool:
        return any(f.tipo == TIPO_MAYOR and f.analizable for f in self.fuentes.values())

    def intervalos_disponibles(self) -> list[str]:
        return [texto_periodo(m.inicio, m.fin) for m in sorted(self.disponibles, key=lambda m: (m.inicio, m.fin))]

    def resumen_origen(self) -> list[dict]:
        """Una fila por adquisición realmente usada: cuántos vehículos se analizaron con ella."""
        cuenta: Counter = Counter()
        datos: dict = {}
        for f in self.fuentes.values():
            if f.analizable:
                cuenta[f.manifiesto] += 1
                datos[f.manifiesto] = f
        filas = [{"manifiesto": ruta, "tipo": datos[ruta].tipo, "adquisicion_inicio": datos[ruta].adquisicion_inicio,
                  "adquisicion_fin": datos[ruta].adquisicion_fin, "vehiculos": n} for ruta, n in cuenta.items()]
        return sorted(filas, key=lambda r: (r["tipo"] != TIPO_EXACTA, r["manifiesto"]))

    def avisos_de_seleccion(self) -> list[str]:
        return [f"{llave}: {a}" for llave, f in self.fuentes.items() for a in f.advertencias]


def analizar_periodo_local(inicio: datetime, fin: datetime, archivo_excel: Path, raiz: Path,
                           zonas: Zonas | None = None, carpeta_manifiestos: Path | None = None
                           ) -> ResultadoAnalisisLocal:
    """Analiza [inicio, fin] con los GPX y manifiestos que ya existen. NUNCA consulta BAANOOL, no necesita
    token y no modifica ningún archivo. Cada LLAVE del catálogo produce un registro."""
    if fin <= inicio:
        raise ValueError("El inicio del periodo debe ser anterior al fin")
    manifiestos, avisos = descubrir_manifiestos(raiz, carpeta_manifiestos)
    llaves = cargar_llaves(archivo_excel)
    fuentes = {l: seleccionar_fuente(l, inicio, fin, manifiestos, raiz) for l in llaves}
    evaluaciones = ordenar_evaluaciones([_evaluar_fuente(f, inicio, fin, raiz, zonas) for f in fuentes.values()])
    catalogo = set(llaves)
    fuera = sorted({l for m in manifiestos if m.cubre(inicio, fin) for l in m.entradas if l not in catalogo})
    return ResultadoAnalisisLocal(
        evaluaciones=evaluaciones, clave_periodo=periodo_key(inicio, fin), inicio=inicio, fin=fin, fuentes=fuentes,
        sin_cobertura=not any(m.cubre(inicio, fin) for m in manifiestos), disponibles=manifiestos, avisos=avisos,
        llaves_fuera_del_catalogo=fuera)


# ---------- presentación (puro) ----------

def filas_movimiento_diario(e: EvaluacionVehiculo, inicio: datetime, fin: datetime) -> list[dict]:
    """Una fila por día calendario del intervalo (y por cualquier día con métrica). Un día SIN puntos no
    muestra distancia ni nivel: ausencia de posiciones no es lo mismo que 0 km observados."""
    if not e.tiene_datos:
        return []
    puntos_por_dia = Counter(p.fecha.astimezone(ZONA_HORARIA).date() for p in e.resultado.puntos)
    dias: set[date] = set(e.distancia_por_dia or {})
    dia = inicio.date()
    while dia <= fin.date():
        dias.add(dia)
        dia += timedelta(days=1)
    filas = []
    for d in sorted(dias):
        n = puntos_por_dia.get(d, 0)
        con_puntos = n > 0
        filas.append({
            "fecha": d, "puntos": n,
            "distancia_km": (e.distancia_por_dia or {}).get(d, 0.0) if con_puntos else None,
            "nivel": (e.nivel_por_dia or {}).get(d) if con_puntos else None,
            "observacion": "" if con_puntos else "Sin posiciones GPS ese día (no implica que estuviera detenido)"})
    return filas


# ---------- estado de sesión ----------

def aplicar_resultado_subperiodo(estado: MutableMapping, resultado: ResultadoAnalisisLocal, resumen, csv: str,
                                 hora: datetime) -> None:
    """Mismas claves que aplicar_resultado_analisis (para que el resto de la interfaz no cambie) más la
    procedencia. No toca los resultados de la adquisición ni los selectores de periodo."""
    estado["evaluaciones"] = resultado.evaluaciones
    estado["resumen"] = resumen
    estado["csv"] = csv
    estado["info"] = {"modo": "PERIODO", "hora": hora, "entrada": "manifiestos locales", "aviso_excel": None,
                      "geofencing": True}
    estado["periodo_analizado"] = {"clave": resultado.clave_periodo, "inicio": resultado.inicio.strftime(FORMATO_FECHA),
                                   "fin": resultado.fin.strftime(FORMATO_FECHA), "modo": "PERIODO"}
    estado["manifiesto_usado"] = None
    estado["origen_datos"] = {"resumen": resultado.resumen_origen(), "usa_mayor": resultado.usa_adquisicion_mayor,
                              "avisos": resultado.avisos + resultado.avisos_de_seleccion(),
                              "solicitudes_baanool": resultado.solicitudes_baanool}
    estado["fuentes_datos"] = resultado.fuentes
    estado["mapas"] = {}
    estado["mapa_abierto"] = None
