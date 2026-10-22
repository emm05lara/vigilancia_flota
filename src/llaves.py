"""Relación LLAVE <-> nombre de carpeta.

Reutiliza la lógica de crear_carpetas_llaves.py. La llave_original siempre sale
del Excel; nunca se reconstruye a partir del nombre de la carpeta.
"""

from pathlib import Path

from crear_carpetas_llaves import limpiar_nombre_carpeta, obtener_llaves


def clave_carpeta(nombre: str) -> str:
    """Forma de comparación de nombres de carpeta (Windows no distingue mayúsculas)."""
    return nombre.casefold()


def buscar_excel_en(carpeta: Path) -> Path:
    """Primer .xlsx de la carpeta, sin preguntar nada."""
    archivos = sorted(p for p in carpeta.glob("*.xlsx") if not p.name.startswith("~$"))
    if not archivos:
        raise FileNotFoundError(f"No hay ningún .xlsx en {carpeta}")
    return archivos[0]


def cargar_llaves(archivo_excel: Path) -> list[str]:
    """Llaves originales del Excel, sin repetir y en el orden del archivo."""
    _hoja, llaves = obtener_llaves(archivo_excel)
    return list(dict.fromkeys(llaves))


def indexar_por_carpeta(llaves: list[str]) -> dict[str, str]:
    """{clave_carpeta -> llave_original}, construido a partir de las llaves del Excel."""
    indice: dict[str, str] = {}
    for llave in llaves:
        clave = clave_carpeta(limpiar_nombre_carpeta(llave))
        if clave and clave not in indice:
            indice[clave] = llave
    return indice


def llave_de_carpeta(nombre_carpeta: str, indice: dict[str, str]) -> str | None:
    """llave_original de una carpeta, o None si no corresponde a ninguna del Excel."""
    return indice.get(clave_carpeta(nombre_carpeta))
