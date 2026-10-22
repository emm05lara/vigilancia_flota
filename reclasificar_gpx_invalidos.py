"""Reclasifica los *.gpx.invalido históricos de entrada/ (etapa 6B-2, cierre). Se ejecuta A MANO.

Un GPX estructuralmente válido con 0 puntos es en realidad SIN_DATOS_PERIODO: BAANOOL respondió para
ese dispositivo y periodo pero no entregó posiciones (la causa es desconocida). Esos archivos se MUEVEN
(no se borran) fuera de entrada/ a salida/diagnostico_gpx_sin_datos/<carpeta del vehículo>/, para que
no estorben y para que una nueva ejecución del lote vuelva a consultar a BAANOOL.

Los realmente inválidos (XML corrupto, no es GPX, trkpt sin ningún punto válido) se dejan donde están.
Solo se tocan archivos *.gpx.invalido: ni *.gpx, ni Excel, ni zonas/, ni nada más. No se sobrescribe.

Uso:

    py reclasificar_gpx_invalidos.py --dry-run     # solo muestra; no modifica nada
    py reclasificar_gpx_invalidos.py               # mueve los SIN_DATOS_PERIODO históricos
"""

import argparse
import sys
from pathlib import Path

from src.reclasificacion import (COLISION, CON_PUNTOS_VALIDOS, CONSERVAR, ERROR_AL_MOVER, GPX_INVALIDO, MOVER,
                                 MOVIDO, SIN_DATOS_PERIODO_HISTORICO, reclasificar)

RAIZ = Path(__file__).resolve().parent


def llaves_por_carpeta(excel: Path) -> dict[str, str]:
    """{carpeta -> LLAVE original}, solo para mostrar. Lectura del Excel; si falla, se omite."""
    try:
        from src.llaves import cargar_llaves, clave_carpeta
        from crear_carpetas_llaves import limpiar_nombre_carpeta

        return {clave_carpeta(limpiar_nombre_carpeta(l)): l for l in cargar_llaves(excel)}
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reclasifica archivos *.gpx.invalido históricos")
    parser.add_argument("--dry-run", action="store_true", help="solo muestra qué haría; no modifica nada")
    parser.add_argument("--entrada", type=Path, default=RAIZ / "entrada")
    parser.add_argument("--destino", type=Path, default=RAIZ / "salida" / "diagnostico_gpx_sin_datos",
                        help="carpeta diagnóstica (por defecto salida/diagnostico_gpx_sin_datos/)")
    parser.add_argument("--excel", type=Path, default=RAIZ / "Lista_Vehiculos.xlsx",
                        help="solo para mostrar la LLAVE de cada carpeta (se lee, no se modifica)")
    args = parser.parse_args()

    if not args.entrada.is_dir():
        print(f"ERROR: no existe la carpeta de entrada: {args.entrada}", file=sys.stderr)
        return 1
    for flujo in (sys.stdout, sys.stderr):
        flujo.reconfigure(errors="backslashreplace")

    items = reclasificar(args.entrada, args.destino, args.dry_run)
    llaves = llaves_por_carpeta(args.excel)

    sin_datos = [i for i in items if i.clasificacion == SIN_DATOS_PERIODO_HISTORICO]
    invalidos = [i for i in items if i.clasificacion == GPX_INVALIDO]
    con_puntos = [i for i in items if i.clasificacion == CON_PUNTOS_VALIDOS]

    print("RECLASIFICACIÓN DE *.gpx.invalido" + ("  (DRY-RUN: no se modificó nada)" if args.dry_run else ""))
    print(f"\n  Entrada:  {args.entrada}")
    print(f"  Destino:  {args.destino}")
    print(f"\n  Archivos *.gpx.invalido encontrados:            {len(items)}")
    print(f"  GPX válidos con 0 puntos (SIN_DATOS_PERIODO):    {len(sin_datos)}")
    print(f"  Realmente inválidos (se dejan donde están):      {len(invalidos)}")
    if con_puntos:
        print(f"  Con puntos válidos (inesperado, se dejan):       {len(con_puntos)}")

    if sin_datos:
        print("\nSIN_DATOS_PERIODO histórico (BAANOOL respondió sin posiciones; causa desconocida):")
        for i in sin_datos:
            llave = llaves.get(i.carpeta.casefold(), "")
            etiqueta = f"{i.carpeta}" + (f"   [LLAVE {llave}]" if llave else "")
            verbo = {MOVER: "se movería a", MOVIDO: "movido a", COLISION: "COLISIÓN, no se mueve:",
                     ERROR_AL_MOVER: "ERROR, no se movió:"}[i.accion]
            print(f"  {etiqueta}\n    {i.origen.name}\n    {verbo} {i.destino}")
            if i.accion in (COLISION, ERROR_AL_MOVER):
                print(f"    {i.detalle}")
    for i in invalidos + con_puntos:
        print(f"\n[{i.clasificacion}] {i.carpeta}\n    {i.origen.name}\n    {i.detalle}\n    se deja donde está")

    movidos = sum(1 for i in items if i.accion == MOVIDO)
    colisiones = sum(1 for i in items if i.accion == COLISION)
    print(f"\n{'Se moverían' if args.dry_run else 'Movidos'}: "
          f"{len(sin_datos) - colisiones if args.dry_run else movidos}   |   colisiones: {colisiones}   |   "
          f"sin cambios: {sum(1 for i in items if i.accion == CONSERVAR)}")
    print("Ningún archivo fue borrado.")
    return 1 if any(i.accion in (COLISION, ERROR_AL_MOVER) for i in items) else 0


if __name__ == "__main__":
    sys.exit(main())
