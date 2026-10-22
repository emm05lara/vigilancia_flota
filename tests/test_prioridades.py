"""Pruebas de prioridad, nivel de movimiento, resumen, filtros y CSV de la flota.

Usan el motor real (analizar_flota) sobre GPX sintéticos en un directorio temporal y
una zona permitida artificial grande, sin internet ni archivos INEGI.

    Zona permitida "A": lon -105..-95, lat 15..25    (~1100 km de alto)
    Fuera "HID":        lon -105..-95, lat 25..30

Ejecutar desde la raíz del proyecto:
    py -m unittest tests.test_prioridades -v
"""

import csv
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import box

from src.analizador import analizar_flota
from src.gpx_parser import ZONA_HORARIA
from src.prioridades import (ALTA, COLUMNAS_CSV, CRITICA, DATOS_ERROR, DATOS_OK, DATOS_PARCIAL,
                             DATOS_SIN_GPX, MOV_ALTO, MOV_BAJO, MOV_MUY_ALTO, MOV_NORMAL, MOVIMIENTO_ALTO,
                             NORMAL, ORDEN_PRIORIDADES, REVISAR, evaluar_flota,
                             filtrar_evaluaciones, generar_csv, nivel_movimiento, resumir_flota)
from src.zonas import Entidad, Zonas

INICIO = datetime(2026, 10, 1, 8, 0, 0, tzinfo=ZONA_HORARIA)
ZONAS = Zonas(
    entidades=[Entidad("A", "Zona A", box(-105, 15, -95, 25)),
               Entidad("HID", "Hidalgo", box(-105, 25, -95, 30))],
    claves_permitidas=("A",), tolerancia_metros=100)
LON = -100.0
PASO = 0.045  # ~5 km por paso de 5 min (~60 km/h)


def ruta_norte(lat_ini: float, pasos: int, desde_min: float = 0) -> list[tuple[float, float, float]]:
    """(minutos, lat, lon): avanza al norte ~5 km cada 5 min."""
    return [(desde_min + 5 * i, lat_ini + PASO * i, LON) for i in range(pasos + 1)]


def escribir_gpx(carpeta: Path, nombre: str, puntos) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    cuerpo = "".join(
        f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>0.0</ele>'
        f'<time>{(INICIO + timedelta(minutes=m)):%Y-%m-%d %H:%M:%S}</time></trkpt>'
        for m, lat, lon in puntos)
    (carpeta / nombre).write_text(
        f'<?xml version="1.0"?><gpx><trk><trkseg>{cuerpo}</trkseg></trk></gpx>', encoding="utf-8")


def construir_flota(raiz: Path) -> None:
    """Una carpeta por caso. El nombre de carpeta no coincide con ninguna LLAVE (índice vacío)."""
    c = lambda n: raiz / n
    escribir_gpx(c("normal"), "a.gpx", ruta_norte(18.0, 24))  # ~120 km: nivel NORMAL (100-300 km/día)
    # salida y regreso: 12 puntos dentro, 3 en Hidalgo, 5 dentro de nuevo
    ida = ruta_norte(24.8, 8)  # cruza lat 25 y llega a ~25.16
    vuelta = [(5 * (9 + i), 24.9 - PASO * i, LON) for i in range(5)]
    escribir_gpx(c("salida_y_regreso"), "a.gpx", ida + vuelta)
    escribir_gpx(c("termina_fuera"), "a.gpx", ruta_norte(24.9, 6))  # termina en Hidalgo
    escribir_gpx(c("movimiento_alto"), "a.gpx", ruta_norte(16.0, 70))  # ~350 km dentro: ALTO (>300)
    escribir_gpx(c("movimiento_muy_alto"), "a.gpx", ruta_norte(16.0, 90))  # ~450 km dentro: MUY ALTO (>400)
    # hueco: 2 h sin información y ~13 km de desplazamiento, todo dentro de la zona
    hueco = ruta_norte(20.0, 3) + ruta_norte(20.0 + PASO * 3 + 0.12, 3, desde_min=15 + 120)
    escribir_gpx(c("con_hueco"), "a.gpx", hueco)
    c("sin_gpx").mkdir()
    escribir_gpx(c("archivo_roto"), "valido.gpx", ruta_norte(20.0, 6))
    (c("archivo_roto") / "roto.gpx").write_text("<gpx><oops", encoding="utf-8")
    c("solo_roto").mkdir()
    (c("solo_roto") / "roto.gpx").write_text("<gpx><oops", encoding="utf-8")


class PruebasNivelMovimiento(unittest.TestCase):
    def test_niveles_en_los_limites_exactos(self):
        # BAJO < 100 | NORMAL 100..300 | ALTO (300, 400] | MUY ALTO > 400 (umbrales operativos Uber/DiDi)
        esperado = [(0, MOV_BAJO), (50, MOV_BAJO), (99.99, MOV_BAJO),
                    (100, MOV_NORMAL), (150, MOV_NORMAL), (250, MOV_NORMAL), (300, MOV_NORMAL),
                    (300.01, MOV_ALTO), (350, MOV_ALTO), (400, MOV_ALTO),
                    (400.01, MOV_MUY_ALTO), (450, MOV_MUY_ALTO)]
        for km, nivel in esperado:
            self.assertEqual(nivel_movimiento(km), nivel, f"{km} km")

    def test_umbrales_configurables(self):
        self.assertEqual(nivel_movimiento(60, alto_km=50, muy_alto_km=100), MOV_ALTO)
        self.assertEqual(nivel_movimiento(60, alto_km=500, muy_alto_km=900, bajo_km=80), MOV_BAJO)
        self.assertEqual(nivel_movimiento(60, alto_km=500, muy_alto_km=900, bajo_km=50), MOV_NORMAL)


class PruebasFlota(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        raiz = Path(cls._tmp.name)
        construir_flota(raiz)
        resultados = analizar_flota(raiz, {}, ZONAS)
        cls.evaluaciones = evaluar_flota(resultados)
        cls.por_carpeta = {e.resultado.nombre_carpeta: e for e in cls.evaluaciones}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def ev(self, nombre):
        return self.por_carpeta[nombre]

    def test_G_normal(self):
        e = self.ev("normal")
        self.assertEqual((e.prioridad, e.nivel_movimiento, e.estado_datos), (NORMAL, MOV_NORMAL, DATOS_OK))
        self.assertEqual(e.motivos, ())

    def test_A_termino_fuera_es_critica(self):
        e = self.ev("termina_fuera")
        self.assertEqual(e.prioridad, CRITICA)
        self.assertTrue(e.termino_fuera)
        self.assertEqual(e.entidades_fuera, ("Hidalgo",))

    def test_B_salida_confirmada_pero_regreso_es_alta(self):
        e = self.ev("salida_y_regreso")
        self.assertEqual(e.prioridad, ALTA)
        self.assertEqual(e.salidas_confirmadas, 1)
        self.assertFalse(e.termino_fuera)

    def test_C_hueco_con_desplazamiento_sin_salida_es_revisar(self):
        e = self.ev("con_hueco")
        self.assertEqual(e.prioridad, REVISAR)
        self.assertEqual(e.huecos_con_desplazamiento, 1)
        self.assertEqual(e.salidas_confirmadas, 0)
        self.assertTrue(any("Hueco de información de 2 h" in m and "desplazamiento mínimo" in m
                            for m in e.motivos), e.motivos)

    def test_D_conflicto_de_timestamp_es_revisar(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            puntos = ruta_norte(20.0, 6) + [(10, 21.0, LON)]  # misma hora que otro punto, ~100 km aparte
            escribir_gpx(raiz / "conflicto", "a.gpx", puntos)
            e = evaluar_flota(analizar_flota(raiz, {}, ZONAS))[0]
        self.assertEqual(e.conflictos_timestamp, 1)
        self.assertEqual(e.prioridad, REVISAR)

    def test_E_350_km_sin_incidencias_es_movimiento_alto(self):
        e = self.ev("movimiento_alto")
        self.assertGreater(e.distancia_observada_km, 300)
        self.assertLess(e.distancia_observada_km, 400)
        self.assertEqual((e.prioridad, e.nivel_movimiento), (MOVIMIENTO_ALTO, MOV_ALTO))

    def test_F_muy_alto_pero_prioridad_movimiento_alto(self):
        e = self.ev("movimiento_muy_alto")
        self.assertGreater(e.distancia_observada_km, 400)
        self.assertEqual((e.prioridad, e.nivel_movimiento), (MOVIMIENTO_ALTO, MOV_MUY_ALTO))

    def test_H_critico_con_mucho_movimiento_sigue_siendo_critica(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            escribir_gpx(raiz / "largo_y_fuera", "a.gpx", ruta_norte(21.5, 90))  # ~450 km, termina > lat 25
            e = evaluar_flota(analizar_flota(raiz, {}, ZONAS))[0]
        self.assertGreater(e.distancia_observada_km, 400)
        self.assertEqual(e.nivel_movimiento, MOV_MUY_ALTO)
        self.assertEqual(e.prioridad, CRITICA)

    def test_A_sin_gpx_es_estado_de_datos_no_prioridad(self):
        e = self.ev("sin_gpx")
        self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
        self.assertIsNone(e.prioridad)  # sin determinar: ni NORMAL ni CRITICA
        self.assertNotIn(e.prioridad, ORDEN_PRIORIDADES)
        self.assertFalse(e.requiere_atencion)
        self.assertTrue(e.problema_de_datos)
        self.assertIsNone(e.distancia_observada_km)  # sin datos != 0 km
        self.assertIsNone(e.nivel_movimiento)
        self.assertIsNone(e.trayectos)
        self.assertIsNone(e.termino_fuera)

    def test_C_gpx_valido_mas_roto_es_parcial_y_revisar(self):
        e = self.ev("archivo_roto")
        self.assertEqual(e.prioridad, REVISAR)
        self.assertEqual(e.estado_datos, DATOS_PARCIAL)
        self.assertEqual(len(e.errores), 1)
        self.assertEqual(e.errores[0][0], "roto.gpx")
        self.assertGreater(e.distancia_observada_km, 0)  # se procesó el GPX válido

    def test_B_solo_gpx_roto_es_error_y_sin_prioridad(self):
        e = self.ev("solo_roto")
        self.assertEqual(e.estado_datos, DATOS_ERROR)
        self.assertIsNone(e.prioridad)
        self.assertNotEqual(e.prioridad, NORMAL)
        self.assertEqual(len(e.errores), 1)
        self.assertIsNone(e.distancia_observada_km)

    def test_D_vehiculo_normal_con_datos_correctos(self):
        e = self.ev("normal")
        self.assertEqual((e.estado_datos, e.prioridad), (DATOS_OK, NORMAL))
        self.assertFalse(e.requiere_atencion)
        self.assertFalse(e.problema_de_datos)

    def test_orden_de_la_tabla(self):
        print()
        print("  orden:", [(e.resultado.nombre_carpeta, e.prioridad, e.estado_datos,
                            None if e.distancia_observada_km is None else round(e.distancia_observada_km))
                           for e in self.evaluaciones], end="")
        con_prioridad = [e for e in self.evaluaciones if e.prioridad]
        sin_prioridad = [e for e in self.evaluaciones if not e.prioridad]
        # Los vehículos con prioridad van primero y ordenados; los sin datos, al final.
        self.assertEqual(self.evaluaciones, con_prioridad + sin_prioridad)
        indices = [ORDEN_PRIORIDADES.index(e.prioridad) for e in con_prioridad]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(con_prioridad[0].prioridad, CRITICA)
        self.assertEqual(con_prioridad[-1].prioridad, NORMAL)
        self.assertEqual({e.estado_datos for e in sin_prioridad}, {DATOS_SIN_GPX, DATOS_ERROR})
        # Dentro de MOVIMIENTO_ALTO: distancia descendente.
        mov = [e for e in self.evaluaciones if e.prioridad == MOVIMIENTO_ALTO]
        self.assertEqual([e.resultado.nombre_carpeta for e in mov], ["movimiento_muy_alto", "movimiento_alto"])

    def test_resumen_de_flota(self):
        r = resumir_flota(self.evaluaciones)
        # Cobertura de datos
        self.assertEqual(r.carpetas_encontradas, 9)
        self.assertEqual(r.con_datos_validos, 6)
        self.assertEqual(r.parciales, 1)  # archivo_roto
        self.assertEqual(r.sin_gpx, 1)
        self.assertEqual(r.con_error, 1)  # solo_roto
        self.assertEqual(r.con_datos_validos + r.parciales + r.sin_gpx + r.con_error, r.carpetas_encontradas)
        # Análisis vehicular: solo los que tienen datos
        self.assertEqual(r.analizados, 7)
        self.assertEqual(r.con_salida_confirmada, 2)  # salida_y_regreso, termina_fuera
        self.assertEqual(r.terminaron_fuera, 1)
        self.assertEqual(r.movimiento_alto, 2)
        self.assertEqual(r.con_huecos, 1)
        self.assertEqual(r.sin_novedad, 1)
        self.assertEqual(sum(r.por_prioridad.values()), r.analizados)  # sin datos no es una prioridad
        self.assertEqual(r.por_prioridad[CRITICA], 1)
        self.assertEqual(r.por_prioridad[REVISAR], 2)

    def test_filtros(self):
        f = lambda **kw: {e.resultado.nombre_carpeta for e in filtrar_evaluaciones(self.evaluaciones, **kw)}
        self.assertEqual(f(prioridades=[CRITICA]), {"termina_fuera"})
        self.assertEqual(f(salida="con"), {"termina_fuera", "salida_y_regreso"})
        self.assertNotIn("sin_gpx", f(salida="sin"))  # sin datos no es "sin salida"
        self.assertIn("normal", f(salida="sin"))
        self.assertEqual(f(termino_fuera="si"), {"termina_fuera"})
        self.assertEqual(f(entidades=["Hidalgo"]), {"termina_fuera", "salida_y_regreso"})
        self.assertEqual(f(niveles=[MOV_MUY_ALTO]), {"movimiento_muy_alto"})
        self.assertEqual(len(f()), 9)

    def test_E_filtro_de_atencion_no_incluye_sin_gpx_ni_error(self):
        f = lambda **kw: {e.resultado.nombre_carpeta for e in filtrar_evaluaciones(self.evaluaciones, **kw)}
        atencion = f(solo_atencion=True)
        self.assertNotIn("sin_gpx", atencion)
        self.assertNotIn("solo_roto", atencion)
        self.assertNotIn("normal", atencion)
        self.assertEqual(atencion, {"termina_fuera", "salida_y_regreso", "con_hueco", "archivo_roto",
                                    "movimiento_alto", "movimiento_muy_alto"})

    def test_F_filtro_de_problemas_de_datos(self):
        f = lambda **kw: {e.resultado.nombre_carpeta for e in filtrar_evaluaciones(self.evaluaciones, **kw)}
        self.assertEqual(f(problemas_datos=True), {"sin_gpx", "solo_roto", "archivo_roto"})  # SIN_GPX/ERROR/PARCIAL
        # Ambos activos: unión de incidencias del vehículo y problemas de datos.
        self.assertEqual(f(solo_atencion=True, problemas_datos=True),
                         f(solo_atencion=True) | f(problemas_datos=True))
        self.assertIn("sin_gpx", f(solo_atencion=True, problemas_datos=True))

    def test_csv_usa_los_resultados_ya_calculados(self):
        texto = generar_csv(self.evaluaciones)
        filas = list(csv.DictReader(io.StringIO(texto)))
        self.assertEqual(list(filas[0].keys()), COLUMNAS_CSV)
        for obligatoria in ("LLAVE", "prioridad", "distancia_estimada_observada", "nivel_movimiento",
                            "trayectos", "salidas_confirmadas", "termino_fuera", "entidades_fuera",
                            "huecos_con_desplazamiento", "conflictos_timestamp", "estado_datos"):
            self.assertIn(obligatoria, COLUMNAS_CSV)
        self.assertEqual(len(filas), 9)
        por = {f["carpeta"]: f for f in filas}
        self.assertEqual(por["termina_fuera"]["prioridad"], CRITICA)
        self.assertEqual(por["termina_fuera"]["termino_fuera"], "SI")
        self.assertEqual(por["termina_fuera"]["entidades_fuera"], "Hidalgo")
        self.assertEqual(por["sin_gpx"]["distancia_estimada_observada"], "")  # vacío, no 0
        self.assertEqual(por["sin_gpx"]["prioridad"], "")  # sin determinar
        self.assertEqual(por["sin_gpx"]["estado_datos"], DATOS_SIN_GPX)
        self.assertEqual(por["solo_roto"]["estado_datos"], DATOS_ERROR)

    def test_sin_zonas_no_se_asume_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            escribir_gpx(raiz / "x", "a.gpx", ruta_norte(20.0, 6))
            e = evaluar_flota(analizar_flota(raiz, {}, None))[0]
        self.assertEqual(e.prioridad, REVISAR)
        self.assertIsNone(e.termino_fuera)

    def test_G_un_solo_punto_fuera_al_final_es_revisar_no_critica(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            escribir_gpx(raiz / "x", "a.gpx", ruta_norte(24.88, 3))  # solo el último punto cruza lat 25
            e = evaluar_flota(analizar_flota(raiz, {}, ZONAS))[0]
        self.assertNotEqual(e.prioridad, CRITICA)
        self.assertEqual(e.prioridad, REVISAR)
        self.assertEqual(e.estado_datos, DATOS_OK)
        self.assertFalse(e.termino_fuera)  # no hay periodo fuera confirmado
        self.assertTrue(e.ultimo_punto_fuera)  # pero el último punto geográfico estaba fuera (diagnóstico)
        self.assertTrue(any("Último punto fuera de zona; salida no confirmada" in m for m in e.motivos), e.motivos)


if __name__ == "__main__":
    unittest.main()
