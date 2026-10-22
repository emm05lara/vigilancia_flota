"""Diagnóstico (etapa 6B-1): dispositivos de BAANOOL y su mapeo contra las LLAVES locales.

Hace UNA consulta (getMyDeviceListData), lee Lista_Vehiculos.xlsx (solo lectura), construye el
mapeo en memoria e imprime el resumen y SOLO las excepciones. No descarga ningún GPX, no escribe
en el Excel y no guarda nada.

El token se lee de la variable de entorno BAANOOL_TOKEN (nunca por argumento). En PowerShell:

    $env:BAANOOL_TOKEN = "<token de la sesión actual>"
    py probar_dispositivos_baanool.py

Opciones:
    --excel RUTA              otro Lista_Vehiculos.xlsx
    --mostrar-coincidencias   lista también las coincidencias exactas (DNO enmascarado)
"""

import argparse
import sys
from pathlib import Path

from src.baanool import ErrorBaanool, enmascarar_dno, obtener_dispositivos, obtener_token
from src.llaves import cargar_llaves
from src.mapeo_baanool import (COINCIDENCIA_EXACTA, CONFLICTO, POSIBLE_COINCIDENCIA, SOLO_BAANOOL, SOLO_LOCAL,
                               mapear_llaves_con_dispositivos)

RAIZ = Path(__file__).resolve().parent


def descripcion_dispositivos(entrada) -> str:
    if not entrada.dispositivos:
        return ""
    return "; ".join(f"name={d.name or '(vacío)'!r} dno={enmascarar_dno(d.dno)} carNum={d.car_num or '-'}"
                     for d in entrada.dispositivos[:3]) + (" ..." if len(entrada.dispositivos) > 3 else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispositivos de BAANOOL y mapeo LLAVE <-> DNO")
    parser.add_argument("--excel", type=Path, default=RAIZ / "Lista_Vehiculos.xlsx")
    parser.add_argument("--mostrar-coincidencias", action="store_true")
    args = parser.parse_args()

    for flujo in (sys.stdout, sys.stderr):  # los mensajes de BAANOOL pueden traer caracteres chinos
        flujo.reconfigure(errors="backslashreplace")

    try:
        token = obtener_token()
        print("Consultando dispositivos en BAANOOL...")
        consulta = obtener_dispositivos(token)
    except ErrorBaanool as exc:
        print(f"\nERROR [{exc.codigo}]: {exc.mensaje}", file=sys.stderr)
        return 1

    try:
        llaves = cargar_llaves(args.excel)
    except Exception as exc:
        print(f"\nERROR al leer {args.excel.name}: {exc}", file=sys.stderr)
        return 1

    mapeo = mapear_llaves_con_dispositivos(llaves, consulta.dispositivos)
    r = mapeo.resumen()
    en_linea = sum(1 for d in consulta.dispositivos if d.online is True)
    fuera_de_linea = sum(1 for d in consulta.dispositivos if d.online is False)
    sin_dato = len(consulta.dispositivos) - en_linea - fuera_de_linea

    print(f"\nBAANOOL (HTTP {consulta.http_status}, status={consulta.status}, resmsg={consulta.resmsg})")
    print(f"  Dispositivos: {r['dispositivos']}")
    print(f"  Online: {en_linea}   Offline: {fuera_de_linea}   Sin dato: {sin_dato}")
    for aviso in consulta.advertencias:
        print(f"  ADVERTENCIA: {aviso}")

    print(f"\nMAPEO ({args.excel.name})")
    print(f"  Locales:                {r['locales']}")
    print(f"  Exactos:                {r['exactas']}")
    print(f"  Posibles coincidencias: {r['posibles']}")
    print(f"  Solo local:             {r['solo_local']}")
    print(f"  Solo BAANOOL:           {r['solo_baanool']}")
    print(f"  Conflictos:             {r['conflictos']}")

    if args.mostrar_coincidencias:
        print("\nCOINCIDENCIAS EXACTAS")
        for e in mapeo.con_estado(COINCIDENCIA_EXACTA):
            d = e.dispositivo
            print(f"  {e.llave} -> dno {enmascarar_dno(d.dno)} | carNum {d.car_num or '-'}")

    excepciones = mapeo.excepciones
    print(f"\nEXCEPCIONES ({len(excepciones)})")
    if not excepciones:
        print("  (ninguna)")
    for estado in (CONFLICTO, POSIBLE_COINCIDENCIA, SOLO_LOCAL, SOLO_BAANOOL):
        grupo = mapeo.con_estado(estado)
        if not grupo:
            continue
        print(f"\n  [{estado}] ({len(grupo)})")
        for e in grupo:
            print(f"    {e.etiqueta}")
            print(f"      motivo: {e.motivo}")
            detalle = descripcion_dispositivos(e) if estado in (CONFLICTO, POSIBLE_COINCIDENCIA, SOLO_BAANOOL) else ""
            if detalle:
                print(f"      dispositivo(s): {detalle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
