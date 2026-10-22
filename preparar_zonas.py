"""Utilidad de UNA SOLA VEZ: descarga las 32 áreas geoestadísticas estatales de INEGI
(Marco Geoestadístico, servicio oficial gaia.inegi.org.mx) y las guarda en
zonas/entidades_mexico.geojson.

No forma parte del análisis: main.py nunca descarga nada. Ejecutar manualmente:

    py preparar_zonas.py

Después, el análisis usa únicamente el archivo local.

El Marco Geoestadístico describe áreas geoestadísticas; es una referencia
geográfica operativa, no una determinación jurídica de límites político-administrativos.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

URL_ENTIDAD = "https://gaia.inegi.org.mx/wscatgeo/v2/geo/mgee/{clave}"
CLAVES = [f"{n:02d}" for n in range(1, 33)]
DESTINO = Path(__file__).resolve().parent / "zonas" / "entidades_mexico.geojson"


def descargar_entidad(clave: str) -> tuple[dict, dict]:
    with urlopen(URL_ENTIDAD.format(clave=clave), timeout=60) as respuesta:
        datos = json.loads(respuesta.read().decode("utf-8"))
    feature = datos["features"][0]
    propiedades = feature["properties"]
    if propiedades["cve_ent"] != clave:
        raise ValueError(f"INEGI devolvió la entidad {propiedades['cve_ent']} al pedir {clave}")
    geometria = feature["geometry"]
    geometria.pop("crs", None)  # coordenadas en lon/lat (EPSG:6365, equivalente a WGS84)
    return {
        "type": "Feature",
        "properties": {
            "cve_ent": propiedades["cve_ent"],
            "nombre": propiedades["nomgeo"],
            "nom_abrev": propiedades.get("nom_abrev"),
        },
        "geometry": geometria,
    }, datos.get("metadatos", {})


def main() -> None:
    features = []
    metadatos = {}
    for clave in CLAVES:
        try:
            feature, metadatos = descargar_entidad(clave)
        except (URLError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise SystemExit(f"ERROR al obtener la entidad {clave}: {exc}\n"
                             "No se escribió ningún archivo.")
        features.append(feature)
        print(f"  {clave} {feature['properties']['nombre']}")

    DESTINO.parent.mkdir(exist_ok=True)
    coleccion = {
        "type": "FeatureCollection",
        "metadatos": {
            **metadatos,
            "origen": "https://gaia.inegi.org.mx/wscatgeo/ (Marco Geoestadístico, áreas geoestadísticas estatales)",
            "descargado": datetime.now().isoformat(timespec="seconds"),
            "sistema_coordenadas": "EPSG:6365 (lon/lat, ITRF2008 ~ WGS84)",
        },
        "features": features,
    }
    DESTINO.write_text(json.dumps(coleccion, ensure_ascii=False), encoding="utf-8")
    print(f"\nEscrito: {DESTINO} ({DESTINO.stat().st_size / 1024:.0f} KB, {len(features)} entidades)")


if __name__ == "__main__":
    sys.exit(main())
