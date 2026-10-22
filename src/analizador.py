"""Une LLAVE -> carpeta -> GPX -> métricas. No imprime nada."""

from dataclasses import dataclass, field
from pathlib import Path

from .calidad_gps import ResumenCalidadGps, analizar_calidad_gps
from .geocercas import ResumenGeofencing, resumir_geofencing
from .gpx_parser import Punto, leer_gpx
from .llaves import llave_de_carpeta
from .metricas import ConflictoTimestamp, ResumenPuntos, consolidar_puntos, resumir_puntos
from .movimiento import ResumenActividad, resumir_actividad
from .zonas import Zonas


@dataclass
class ResultadoGpx:
    archivo: Path
    resumen: ResumenPuntos | None = None  # None si hubo error
    actividad: ResumenActividad | None = None
    geofencing: ResumenGeofencing | None = None  # None si no se pasaron zonas
    calidad_gps: ResumenCalidadGps | None = None  # diagnóstico; no afecta las métricas oficiales
    puntos: list[Punto] = field(default_factory=list)  # consolidados dentro del archivo
    puntos_descartados: int = 0  # datos inválidos en el XML
    duplicados_eliminados: int = 0
    conflictos_timestamp: list[ConflictoTimestamp] = field(default_factory=list)
    error: str | None = None


@dataclass
class ResultadoVehiculo:
    carpeta: Path
    nombre_carpeta: str
    llave_original: str | None  # None: la carpeta no corresponde a ninguna LLAVE del Excel
    gpx: list[ResultadoGpx] = field(default_factory=list)
    # Resumen del vehículo: puntos de todos sus GPX combinados y consolidados.
    resumen: ResumenPuntos | None = None
    actividad: ResumenActividad | None = None
    geofencing: ResumenGeofencing | None = None  # None si no se pasaron zonas
    calidad_gps: ResumenCalidadGps | None = None  # diagnóstico; no afecta las métricas oficiales
    puntos: list[Punto] = field(default_factory=list)
    duplicados_eliminados: int = 0
    conflictos_timestamp: list[ConflictoTimestamp] = field(default_factory=list)

    @property
    def numero_gpx(self) -> int:
        return len(self.gpx)


def listar_gpx(carpeta: Path) -> list[Path]:
    return sorted(p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() == ".gpx")


def analizar_gpx(ruta: Path, zonas: Zonas | None = None) -> ResultadoGpx:
    try:
        puntos, descartados = leer_gpx(ruta)
    except Exception as exc:  # un GPX dañado no debe detener el lote
        return ResultadoGpx(ruta, error=f"{type(exc).__name__}: {exc}")

    consolidacion = consolidar_puntos(puntos)
    return ResultadoGpx(
        archivo=ruta,
        resumen=resumir_puntos(consolidacion.puntos),
        actividad=resumir_actividad(consolidacion.puntos),
        geofencing=resumir_geofencing(consolidacion.puntos, zonas) if zonas else None,
        calidad_gps=analizar_calidad_gps(consolidacion.puntos, consolidacion.conflictos, zonas),
        puntos=consolidacion.puntos,
        puntos_descartados=descartados,
        duplicados_eliminados=consolidacion.duplicados_eliminados,
        conflictos_timestamp=consolidacion.conflictos,
    )


def analizar_carpeta(carpeta: Path, indice_llaves: dict[str, str],
                     zonas: Zonas | None = None) -> ResultadoVehiculo:
    """Modo local: analiza TODOS los .gpx de la carpeta de un vehículo."""
    return analizar_archivos(carpeta, listar_gpx(carpeta), llave_de_carpeta(carpeta.name, indice_llaves), zonas)


def analizar_archivos(carpeta: Path, archivos: list[Path], llave_original: str | None,
                      zonas: Zonas | None = None) -> ResultadoVehiculo:
    """Analiza únicamente los archivos indicados (en el orden dado) como un vehículo.

    Es el análisis de siempre; el modo por periodo BAANOOL lo usa con el archivo exacto del manifiesto
    para no mezclar periodos.
    """
    resultado = ResultadoVehiculo(
        carpeta=carpeta,
        nombre_carpeta=carpeta.name,
        llave_original=llave_original,
    )

    todos: list[Punto] = []
    for ruta in archivos:
        resultado_gpx = analizar_gpx(ruta)
        resultado.gpx.append(resultado_gpx)
        todos.extend(resultado_gpx.puntos)
        # Lo detectado dentro de cada archivo y lo detectado entre archivos no se repite:
        # al consolidar la carpeta, los puntos ya limpiados por archivo no vuelven a contarse.
        resultado.duplicados_eliminados += resultado_gpx.duplicados_eliminados
        resultado.conflictos_timestamp.extend(resultado_gpx.conflictos_timestamp)

    if resultado.gpx:
        consolidacion = consolidar_puntos(todos)
        resultado.puntos = consolidacion.puntos
        resultado.duplicados_eliminados += consolidacion.duplicados_eliminados
        resultado.conflictos_timestamp.extend(consolidacion.conflictos)
        resultado.resumen = resumir_puntos(resultado.puntos)
        resultado.actividad = resumir_actividad(resultado.puntos)
        if zonas:  # sobre los puntos ya consolidados de todos los GPX del vehículo
            resultado.geofencing = resumir_geofencing(resultado.puntos, zonas)
        resultado.calidad_gps = analizar_calidad_gps(resultado.puntos, resultado.conflictos_timestamp, zonas)
    return resultado


def analizar_flota(carpeta_entrada: Path, indice_llaves: dict[str, str],
                   zonas: Zonas | None = None) -> list[ResultadoVehiculo]:
    """Una carpeta de primer nivel dentro de entrada/ = un vehículo."""
    carpetas = sorted(p for p in carpeta_entrada.iterdir() if p.is_dir())
    return [analizar_carpeta(c, indice_llaves, zonas) for c in carpetas]
