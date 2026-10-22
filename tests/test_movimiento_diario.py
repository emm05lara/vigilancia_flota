"""Pruebas del nivel de movimiento por día calendario (America/Mexico_City).

El nivel se evalúa por día (BAJO <100, NORMAL 100-300, ALTO >300, MUY ALTO >400 km) y el nivel del
periodo es el máximo diario. La distancia total no cambia.

    py -m unittest tests.test_movimiento_diario -v
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from src.analizador import analizar_flota
from src.gpx_parser import ZONA_HORARIA, Punto
from src.metricas import calcular_tramos, haversine_km
from src.movimiento import distancia_por_dia
from src.prioridades import (CRITICA, MOV_ALTO, MOV_BAJO, MOV_MUY_ALTO, MOV_NORMAL, MOVIMIENTO_ALTO, NORMAL,
                             evaluar_flota)
from tests.test_prioridades import INICIO, LON, PASO, ZONAS, escribir_gpx, ruta_norte

DIA = 24 * 60  # minutos


def evaluar(*partes):
    """Un GPX con las partes concatenadas (cada parte: lista (minutos, lat, lon))."""
    puntos = [p for parte in partes for p in parte]
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        escribir_gpx(raiz / "v", "a.gpx", puntos)
        e = evaluar_flota(analizar_flota(raiz, {}, ZONAS))[0]
    return e


def dias_seguidos(*pasos_por_dia, lat_ini=16.0):
    """Un bloque de ruta al norte por día (08:00 de cada día), continuando donde terminó el anterior."""
    partes, lat = [], lat_ini
    for n, pasos in enumerate(pasos_por_dia):
        partes.append(ruta_norte(lat, pasos, desde_min=n * DIA))
        lat += PASO * pasos
    return partes


class PruebasMovimientoDiario(unittest.TestCase):
    def mostrar(self, caso, e):
        dias = ", ".join(f"{d:%m-%d}={km:.0f}({e.nivel_por_dia[d]})" for d, km in e.distancia_por_dia.items())
        print(f"\n  [{caso}] total={e.distancia_observada_km:.0f} km | {dias} | periodo={e.nivel_movimiento} "
              f"| prioridad={e.prioridad}", end="")

    def test_A_410_km_en_un_dia_es_muy_alto(self):
        e = evaluar(*dias_seguidos(82))
        self.mostrar("A", e)
        self.assertAlmostEqual(e.distancia_observada_km, 410.3, delta=0.5)
        self.assertEqual(e.nivel_movimiento, MOV_MUY_ALTO)
        self.assertEqual(e.prioridad, MOVIMIENTO_ALTO)

    def test_B_130_mas_130_en_dos_dias_es_normal(self):
        e = evaluar(*dias_seguidos(26, 26))
        self.mostrar("B", e)
        self.assertAlmostEqual(e.distancia_observada_km, 260.2, delta=0.5)  # el total no cambia
        self.assertEqual(len(e.distancia_por_dia), 2)
        self.assertEqual(e.nivel_movimiento, MOV_NORMAL)
        self.assertEqual(e.prioridad, NORMAL)

    def test_C_350_mas_350_es_alto_no_muy_alto(self):
        e = evaluar(*dias_seguidos(70, 70))
        self.mostrar("C", e)
        self.assertAlmostEqual(e.distancia_observada_km, 700.6, delta=1)  # el total acumulado no clasifica
        self.assertEqual(e.nivel_movimiento, MOV_ALTO)
        self.assertEqual(e.prioridad, MOVIMIENTO_ALTO)

    def test_D_410_mas_20_es_muy_alto(self):
        e = evaluar(*dias_seguidos(82, 4))
        self.mostrar("D", e)
        self.assertEqual(e.nivel_movimiento, MOV_MUY_ALTO)
        self.assertAlmostEqual(e.distancia_maxima_diaria_km, 410.3, delta=0.5)
        self.assertEqual(e.fecha_distancia_maxima, date(2026, 10, 1))
        self.assertEqual(list(e.nivel_por_dia.values()), [MOV_MUY_ALTO, MOV_BAJO])

    def test_300_km_en_tres_dias_de_100_es_normal(self):
        e = evaluar(*dias_seguidos(20, 20, 20))
        self.mostrar("3x100", e)
        self.assertGreater(e.distancia_observada_km, 290)
        self.assertEqual(e.nivel_movimiento, MOV_NORMAL)
        self.assertEqual(e.prioridad, NORMAL)  # sin otras incidencias

    def test_periodo_es_el_maximo_diario_no_el_promedio(self):
        # 350 (ALTO), 350 (ALTO), 50 (BAJO) -> ALTO; el promedio (250, NORMAL) lo habría ocultado.
        e = evaluar(*dias_seguidos(70, 70, 10))
        self.mostrar("max", e)
        self.assertEqual(list(e.nivel_por_dia.values()), [MOV_ALTO, MOV_ALTO, MOV_BAJO])
        self.assertEqual(e.nivel_movimiento, MOV_ALTO)

    def test_dia_sin_distancia_aparece_con_cero(self):
        # Día 1 con movimiento; día 2 estacionado (2 puntos); día 3 con movimiento.
        parte1 = ruta_norte(16.0, 10)
        lat_fin = 16.0 + PASO * 10
        parqueado = [(DIA + 600, lat_fin, LON), (DIA + 605, lat_fin, LON)]
        parte3 = ruta_norte(lat_fin, 10, desde_min=2 * DIA)
        e = evaluar(parte1, parqueado, parte3)
        self.mostrar("cero", e)
        self.assertEqual(len(e.distancia_por_dia), 3)
        self.assertAlmostEqual(list(e.distancia_por_dia.values())[1], 0.0, places=6)
        self.assertEqual(list(e.nivel_por_dia.values()), [MOV_BAJO, MOV_BAJO, MOV_BAJO])
        self.assertEqual(e.nivel_movimiento, MOV_BAJO)
        self.assertEqual(e.prioridad, NORMAL)  # BAJO es descriptivo: no genera prioridad

    def test_E_tramo_que_cruza_medianoche_se_reparte_sin_duplicarse(self):
        # 23:58 -> 00:03 (5 min, ~5 km): 2/5 para el día 1 y 3/5 para el día 2.
        t0 = datetime(2026, 10, 1, 23, 58, 0, tzinfo=ZONA_HORARIA)
        a = Punto(t0, 19.0, LON, 0.0)
        b = Punto(t0 + timedelta(minutes=5), 19.045, LON, 0.0)
        tramos = calcular_tramos([a, b])
        total = haversine_km(a.latitud, a.longitud, b.latitud, b.longitud)
        dias = distancia_por_dia([a, b], tramos)
        print(f"\n  [E] total={total:.3f} km | {{{', '.join(f'{d}: {km:.3f}' for d, km in dias.items())}}}", end="")
        self.assertEqual(list(dias), [date(2026, 10, 1), date(2026, 10, 2)])
        self.assertAlmostEqual(dias[date(2026, 10, 1)], total * 2 / 5, places=9)
        self.assertAlmostEqual(dias[date(2026, 10, 2)], total * 3 / 5, places=9)
        self.assertAlmostEqual(sum(dias.values()), total, places=9)  # no se duplica

    def test_F_suma_diaria_coincide_con_el_total(self):
        # Ruta continua de 23:00 a 02:00 (cruza medianoche) y otra con varios días.
        base = ruta_norte(16.0, 40, desde_min=15 * 60)  # empieza 23:00 locales (INICIO es 08:00)
        e = evaluar(base, *dias_seguidos(10, 20, 15, lat_ini=18.0)[1:])
        self.mostrar("F", e)
        self.assertGreaterEqual(len(e.distancia_por_dia), 2)
        self.assertAlmostEqual(sum(e.distancia_por_dia.values()), e.distancia_observada_km, places=6)

    def test_G_critico_con_movimiento_muy_alto_sigue_siendo_critica(self):
        e = evaluar(ruta_norte(21.5, 90))  # ~450 km en un día y termina fuera de la zona (lat > 25)
        self.mostrar("G", e)
        self.assertEqual(e.nivel_movimiento, MOV_MUY_ALTO)
        self.assertEqual(e.prioridad, CRITICA)

    def test_bajo_no_produce_movimiento_alto(self):
        e = evaluar(*dias_seguidos(10))  # ~50 km
        self.mostrar("BAJO", e)
        self.assertEqual((e.nivel_movimiento, e.prioridad), (MOV_BAJO, NORMAL))
        self.assertFalse(e.requiere_atencion)

    def test_normal_no_produce_movimiento_alto(self):
        for pasos in (20, 40, 59):  # ~100, ~200 y ~295 km
            e = evaluar(*dias_seguidos(pasos))
            self.assertEqual((e.nivel_movimiento, e.prioridad), (MOV_NORMAL, NORMAL), pasos)

    def test_alto_produce_movimiento_alto_si_no_hay_prioridad_superior(self):
        e = evaluar(*dias_seguidos(62))  # ~310 km
        self.mostrar("ALTO", e)
        self.assertEqual((e.nivel_movimiento, e.prioridad), (MOV_ALTO, MOVIMIENTO_ALTO))
        self.assertTrue(e.requiere_atencion)

    def test_muy_alto_produce_movimiento_alto_si_no_hay_prioridad_superior(self):
        e = evaluar(*dias_seguidos(82))  # ~410 km
        self.assertEqual((e.nivel_movimiento, e.prioridad), (MOV_MUY_ALTO, MOVIMIENTO_ALTO))

    def test_distancia_total_no_cambia_respecto_a_la_suma_de_tramos_normales(self):
        e = evaluar(*dias_seguidos(30, 30))
        tramos = calcular_tramos(e.resultado.puntos)
        suma = sum(t.distancia_km for t in tramos if t.tipo == "tramo_normal")
        self.assertAlmostEqual(e.distancia_observada_km, suma, places=9)


if __name__ == "__main__":
    unittest.main()
