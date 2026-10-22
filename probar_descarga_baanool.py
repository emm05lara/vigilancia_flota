"""Prueba (etapa 6A) de descarga de UN GPX desde BAANOOL. Solo consola.

El token NO se pasa por argumento (quedaría en el historial de comandos): se lee de la variable
de entorno BAANOOL_TOKEN. En PowerShell:

    $env:BAANOOL_TOKEN = "<token de la sesión actual>"
    py probar_descarga_baanool.py --dno <DNO> --inicio "2026-10-12 10:00:08" --fin "2026-10-12 10:50:08"

--inicio y --fin se dan SIEMPRE en hora local de Ciudad de México; el programa los convierte a la
hora que espera BAANOOL (UTC+8) y lo muestra antes de enviar. El nombre del archivo y los
timestamps del GPX quedan en la hora que ya usa el sistema.

Opcional: --lat y --lng (juntos) agregan el header Referer de la página playGoogle a la
solicitud de exportación, como hace el navegador.

Opcional: --user-agent-navegador agrega el header User-Agent de Chrome (constante
USER_AGENT_BAANOOL) a esa misma solicitud. Se cambia una variable por prueba.

Opcional, para comparar de forma diagnóstica con un GPX exportado manualmente:

    ... --comparar ruta\\al\\gpx_manual.gpx

El archivo se guarda en descargas_prueba/ (separado de entrada/). Este script no analiza la
flota ni modifica entrada/.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.baanool import (FORMATO_FECHA, USER_AGENT_BAANOOL, ErrorBaanool, convertir_fecha_local_a_baanool,
                         descargar_gpx_de_baanool, obtener_token)
from src.gpx_parser import leer_gpx

RAIZ = Path(__file__).resolve().parent


def fecha(texto: str) -> datetime:
    try:
        return datetime.strptime(texto, FORMATO_FECHA)
    except ValueError:
        raise argparse.ArgumentTypeError(f"fecha inválida {texto!r}: usa el formato 'YYYY-MM-DD HH:MM:SS'")


def comparar_con_manual(descargado: Path, manual: Path) -> None:
    """Solo diagnóstico: no se exige que los archivos sean idénticos."""
    try:
        puntos_manual, _ = leer_gpx(manual)
    except Exception as exc:
        print(f"  (No se pudo leer el GPX manual: {type(exc).__name__})")
        return
    puntos_descargados, _ = leer_gpx(descargado)
    if not puntos_manual:
        print("  (El GPX manual no tiene puntos)")
        return

    def resumen(puntos):
        a, z = puntos[0], puntos[-1]
        return (len(puntos), f"{a.fecha:%Y-%m-%d %H:%M:%S}", f"{z.fecha:%Y-%m-%d %H:%M:%S}",
                f"{a.latitud:.6f}, {a.longitud:.6f}", f"{z.latitud:.6f}, {z.longitud:.6f}")

    filas = ["Puntos", "Primer timestamp", "Último timestamp", "Coordenada inicial", "Coordenada final"]
    d, m = resumen(puntos_descargados), resumen(puntos_manual)
    print("\nComparación diagnóstica (descargado vs. manual):")
    print(f"  {'':<20}{'descargado':<28}{'manual':<28}")
    for etiqueta, a, b in zip(filas, d, m):
        marca = "" if a == b else "  <- difiere"
        print(f"  {etiqueta:<20}{str(a):<28}{str(b):<28}{marca}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Descarga de prueba de UN GPX desde BAANOOL")
    parser.add_argument("--dno", required=True, help="DNO del dispositivo")
    parser.add_argument("--inicio", required=True, type=fecha, help="'YYYY-MM-DD HH:MM:SS' (hora CDMX)")
    parser.add_argument("--fin", required=True, type=fecha, help="'YYYY-MM-DD HH:MM:SS' (hora CDMX)")
    parser.add_argument("--lat", type=float, help="latitud para el Referer (requiere --lng)")
    parser.add_argument("--lng", type=float, help="longitud para el Referer (requiere --lat)")
    parser.add_argument("--user-agent-navegador", action="store_true",
                        help="enviar el User-Agent de navegador (USER_AGENT_BAANOOL) en la exportación")
    parser.add_argument("--destino", type=Path, default=RAIZ / "descargas_prueba",
                        help="carpeta de salida (por defecto descargas_prueba/)")
    parser.add_argument("--comparar", type=Path, help="GPX manual del mismo DNO/rango, solo diagnóstico")
    args = parser.parse_args()
    if (args.lat is None) != (args.lng is None):
        parser.error("--lat y --lng deben indicarse juntos")

    # Los mensajes de BAANOOL pueden traer caracteres chinos: la consola nunca debe fallar al imprimirlos.
    for flujo in (sys.stdout, sys.stderr):
        flujo.reconfigure(errors="backslashreplace")

    try:
        fmt = lambda d: d.strftime(FORMATO_FECHA)
        print("Periodo solicitado (CDMX):")
        print(f"  {fmt(args.inicio)}")
        print(f"  {fmt(args.fin)}")
        print("Periodo enviado a BAANOOL:")
        print(f"  {fmt(convertir_fecha_local_a_baanool(args.inicio))}")
        print(f"  {fmt(convertir_fecha_local_a_baanool(args.fin))}")
        print("Referer: " + ("se enviará (playGoogle con dno/lng/lat)" if args.lat is not None
                             else "no se enviará"))
        print("User-Agent de navegador: " + ("se enviará" if args.user_agent_navegador
                                             else "no se enviará (el predeterminado de requests)"))
        obtener_token()  # falla pronto y con mensaje claro si no hay token; no hace ninguna petición
        resultado = descargar_gpx_de_baanool(args.dno, args.inicio, args.fin, args.destino,
                                             avisar=lambda mensaje: print(mensaje),
                                             latitud=args.lat, longitud=args.lng,
                                             user_agent=USER_AGENT_BAANOOL if args.user_agent_navegador else None)
    except ErrorBaanool as exc:
        print(f"\nERROR [{exc.codigo}]: {exc.mensaje}", file=sys.stderr)
        return 1

    e = resultado.exportacion
    print("\nResultado")
    print(f"  Exportación: HTTP {e.http_status} | status={e.status} | resmsg={e.resmsg}")
    print(f"  Archivo: {resultado.ruta}")
    print(f"  Tamaño: {resultado.tamano_bytes} bytes")
    print(f"  Puntos válidos: {resultado.puntos_validos} (descartados: {resultado.puntos_descartados})")
    print(f"  Primer timestamp: {resultado.primer_timestamp:%Y-%m-%d %H:%M:%S}")
    print(f"  Último timestamp: {resultado.ultimo_timestamp:%Y-%m-%d %H:%M:%S}")
    print(f"  Coordenada inicial: {resultado.coordenada_inicial[0]:.6f}, {resultado.coordenada_inicial[1]:.6f}")
    print(f"  Coordenada final: {resultado.coordenada_final[0]:.6f}, {resultado.coordenada_final[1]:.6f}")
    if args.comparar:
        comparar_con_manual(resultado.ruta, args.comparar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
