"""Pruebas de calidad GPS: velocidad sospechosa vs. salto GPS probable (solo diagnóstico).

Geometría sintética cerca de la CDMX; las posiciones se dan en km hacia el norte/este de un
origen. Sin internet ni archivos INEGI.

    py -m unittest tests.test_calidad_gps -v
"""

import dataclasses
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.analizador import analizar_flota
from src.calidad_gps import (CALIDAD_REVISAR, CALIDAD_SIN_ALERTAS, TIPO_SALTO_PROBABLE, analizar_calidad_gps,
                             evaluar_secuencia)
from src.gpx_parser import ZONA_HORARIA, Punto
from src.metricas import ConflictoTimestamp, calcular_tramos, haversine_km, resumir_puntos
from src.movimiento import distancia_por_dia
from src.prioridades import evaluar_vehiculo
from tests.test_prioridades import ZONAS

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=ZONA_HORARIA)
LAT0, LON0 = 19.40, -99.10
KM_LAT = 1 / 111.195
KM_LON = 1 / (111.195 * math.cos(math.radians(LAT0)))


def pto(seg: float, norte_km: float, este_km: float = 0.0) -> Punto:
    return Punto(T0 + timedelta(seconds=seg), LAT0 + norte_km * KM_LAT, LON0 + este_km * KM_LON, 0.0)


def ruta_recta(inicio_s, cantidad, paso_s=10, kmh=60.0, norte0=0.0, este_km=0.0):
    """Puntos hacia el norte a velocidad constante."""
    paso_km = kmh * paso_s / 3600
    return [pto(inicio_s + i * paso_s, norte0 + i * paso_km, este_km) for i in range(cantidad)]


def con_salto(desvio_este_km=2.0, indice=5, cantidad=11, paso_s=6, kmh=60.0):
    """Ruta recta con el punto `indice` desplazado hacia el este (patrón A-X-B)."""
    puntos = ruta_recta(0, cantidad, paso_s, kmh)
    x = puntos[indice]
    puntos[indice] = Punto(x.fecha, x.latitud, x.longitud + desvio_este_km * KM_LON, 0.0)
    return puntos


def escribir_gpx(carpeta: Path, nombre: str, puntos) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    cuerpo = "".join(
        f'<trkpt lat="{p.latitud:.6f}" lon="{p.longitud:.6f}"><ele>0.0</ele>'
        f'<time>{p.fecha:%Y-%m-%d %H:%M:%S}</time></trkpt>' for p in puntos)
    (carpeta / nombre).write_text(
        f'<?xml version="1.0"?><gpx><trk><trkseg>{cuerpo}</trkseg></trk></gpx>', encoding="utf-8")


def analizar_vehiculo(puntos, zonas=None):
    """El vehículo completo por el motor real (carpeta -> GPX -> consolidación -> métricas)."""
    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        escribir_gpx(raiz / "v", "a.gpx", puntos)
        return analizar_flota(raiz, {}, zonas)[0]


class PruebasCalidadGps(unittest.TestCase):
    def mostrar(self, caso, c):
        print(f"\n  [{caso}] estado={c.estado} sospechosos={len(c.tramos_velocidad_sospechosa)} "
              f"saltos={len(c.saltos_probables)} complejas={len(c.anomalias_complejas)} "
              f"oficial={c.distancia_oficial_km:.3f} depurada={c.distancia_depurada_km:.3f}", end="")

    # ---------- A, B ----------

    def test_A_trayectoria_normal_sin_alertas(self):
        c = analizar_calidad_gps(ruta_recta(0, 40, paso_s=10, kmh=60))
        self.mostrar("A", c)
        self.assertEqual(c.estado, CALIDAD_SIN_ALERTAS)
        self.assertEqual((len(c.tramos_velocidad_sospechosa), len(c.saltos_probables),
                          len(c.anomalias_complejas)), (0, 0, 0))
        self.assertAlmostEqual(c.distancia_depurada_km, c.distancia_oficial_km)

    def test_B_170_kmh_coherente_es_velocidad_sospechosa_no_salto(self):
        # Tramo recto a 170 km/h (60 s, 2.83 km) entre dos tramos normales: físicamente posible.
        antes = ruta_recta(0, 4, paso_s=10, kmh=60)  # termina en t=30 s, 0.5 km al norte
        km_tramo = 170 * 60 / 3600
        despues = ruta_recta(30 + 60, 4, paso_s=10, kmh=60, norte0=0.5 + km_tramo)
        c = analizar_calidad_gps(antes + despues)
        self.mostrar("B", c)
        self.assertEqual(len(c.tramos_velocidad_sospechosa), 1)
        self.assertAlmostEqual(c.tramos_velocidad_sospechosa[0].velocidad_estimada_kmh, 170, delta=1)
        self.assertEqual(c.tramos_velocidad_sospechosa[0].relacionado_con, None)
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(c.anomalias_complejas, [])
        self.assertEqual(c.estado, CALIDAD_REVISAR)
        self.assertAlmostEqual(c.distancia_depurada_km, c.distancia_oficial_km)  # el tramo se conserva

    # ---------- C, G ----------

    def test_C_patron_a_x_b_es_un_salto_probable(self):
        c = analizar_calidad_gps(con_salto())
        self.mostrar("C", c)
        self.assertEqual(len(c.saltos_probables), 1)
        s = c.saltos_probables[0]
        self.assertEqual(s.punto_sospechoso.fecha, T0 + timedelta(seconds=30))
        self.assertAlmostEqual(s.distancia_ax_km, 2.0, delta=0.05)
        self.assertGreater(s.velocidad_ax_kmh, 300)
        self.assertGreaterEqual(s.relacion_desvio, 4.0)
        self.assertLess(s.velocidad_ab_kmh, 160)
        self.assertEqual(len(s.motivos), 5)
        self.assertEqual(c.anomalias_complejas, [])
        self.assertTrue(all(t.relacionado_con == TIPO_SALTO_PROBABLE for t in c.tramos_velocidad_sospechosa))
        self.assertEqual(c.puntos_excluidos_experimental, (s.punto_sospechoso,))

    def test_G_distancia_depurada_exacta_sin_doble_conteo(self):
        # Dos saltos aislados en la misma ruta: cada tramo se descuenta una sola vez.
        puntos = ruta_recta(0, 21, paso_s=6, kmh=60)
        for i in (5, 15):
            x = puntos[i]
            puntos[i] = Punto(x.fecha, x.latitud, x.longitud + 1.5 * KM_LON, 0.0)
        c = analizar_calidad_gps(puntos)
        self.mostrar("G", c)
        self.assertEqual(len(c.saltos_probables), 2)
        esperado = c.distancia_oficial_km
        for s in c.saltos_probables:
            esperado = esperado - s.distancia_ax_km - s.distancia_xb_km + s.distancia_ab_km
        self.assertAlmostEqual(c.distancia_depurada_km, esperado, places=9)
        # Verificación independiente: recalcular sobre la ruta sin los puntos X.
        sin_x = [p for p in puntos if p not in c.puntos_excluidos_experimental]
        self.assertEqual(len(sin_x), len(puntos) - 2)
        independiente = resumir_puntos(sin_x).distancia_estimada_observada_km
        self.assertAlmostEqual(c.distancia_depurada_km, independiente, places=9)
        self.assertAlmostEqual(c.distancia_atribuida_saltos_km, c.distancia_oficial_km - independiente, places=9)

    # ---------- D, E ----------

    def test_D_punto_lejano_sin_regreso_no_se_corrige(self):
        # La posición salta 2 km y la trayectoria continúa desde allí: no hay retorno coherente.
        antes = ruta_recta(0, 5, paso_s=6, kmh=60)
        despues = ruta_recta(30, 6, paso_s=6, kmh=60, norte0=0.5, este_km=2.0)
        c = analizar_calidad_gps(antes + despues)
        self.mostrar("D", c)
        self.assertGreaterEqual(len(c.tramos_velocidad_sospechosa), 1)  # velocidad sospechosa / revisar
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(c.anomalias_complejas, [])
        self.assertAlmostEqual(c.distancia_depurada_km, c.distancia_oficial_km)  # sin corrección automática
        self.assertEqual(c.estado, CALIDAD_REVISAR)

    def test_E_registros_separados_por_minutos_no_son_salto(self):
        # A, X (30 km al este, 5 min después) y B (5 min después): >120 s entre A y B.
        puntos = [pto(0, 0.0), pto(300, 0.0, 30.0), pto(600, 0.1)]
        c = analizar_calidad_gps(puntos)
        self.mostrar("E", c)
        e = evaluar_secuencia(puntos, 0, 2)
        self.assertFalse(e.condiciones["ventana_corta"])
        self.assertGreater(e.relacion_desvio, 4)
        self.assertGreater(e.velocidad_ax_kmh, 300)
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(c.anomalias_complejas, [])

    # ---------- F, P, Q ----------

    def test_F_dos_puntos_consecutivos_son_anomalia_compleja(self):
        puntos = ruta_recta(0, 12, paso_s=6, kmh=60)
        for i, extra in ((5, 0.0), (6, 0.1)):  # dos puntos consecutivos desplazados ~2 km
            x = puntos[i]
            puntos[i] = Punto(x.fecha, x.latitud, x.longitud + (2.0 + extra) * KM_LON, 0.0)
        c = analizar_calidad_gps(puntos)
        self.mostrar("F", c)
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(len(c.anomalias_complejas), 1)
        self.assertEqual(len(c.anomalias_complejas[0].puntos_sospechosos), 2)
        self.assertAlmostEqual(c.distancia_depurada_km, c.distancia_oficial_km)  # sin corrección automática
        self.assertEqual(c.puntos_excluidos_experimental, ())
        self.assertEqual(c.estado, CALIDAD_REVISAR)

    def test_P_dos_saltos_que_comparten_un_punto_son_complejos(self):
        # A X1 B X2 C: B pertenece a ambos candidatos -> no es un caso aislado claro.
        puntos = ruta_recta(0, 11, paso_s=6, kmh=60)
        for i in (4, 6):
            x = puntos[i]
            puntos[i] = Punto(x.fecha, x.latitud, x.longitud + 2.0 * KM_LON, 0.0)
        c = analizar_calidad_gps(puntos)
        self.mostrar("P", c)
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(len(c.anomalias_complejas), 1)
        self.assertAlmostEqual(c.distancia_depurada_km, c.distancia_oficial_km)

    def test_Q_conflicto_de_timestamp_cercano_hace_compleja_la_anomalia(self):
        puntos = con_salto()
        conflicto = ConflictoTimestamp(T0 + timedelta(seconds=31), (LAT0, LON0), (LAT0 + 0.01, LON0), 1100.0)
        c = analizar_calidad_gps(puntos, [conflicto])
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(len(c.anomalias_complejas), 1)
        self.assertIn("Conflicto de timestamp en el entorno", c.anomalias_complejas[0].motivos)

    # ---------- H, K, L, J: lo oficial no cambia ----------

    def test_H_distancia_oficial_no_cambia(self):
        puntos = con_salto()
        v = analizar_vehiculo(puntos)
        c = v.calidad_gps
        self.assertEqual(len(c.saltos_probables), 1)
        # Línea base: lo que el motor oficial calcula sobre los puntos consolidados (el GPX redondea
        # a 6 decimales, por eso no se compara contra los puntos en memoria sin redondear).
        oficial = resumir_puntos(v.puntos).distancia_estimada_observada_km
        self.assertAlmostEqual(v.resumen.distancia_estimada_observada_km, oficial, places=9)
        self.assertAlmostEqual(c.distancia_oficial_km, oficial, places=9)
        self.assertLess(c.distancia_depurada_km, oficial)
        # Los puntos originales siguen intactos y completos (incluido el sospechoso).
        self.assertEqual(len(v.puntos), len(puntos))
        x = c.saltos_probables[0].punto_sospechoso
        self.assertIn(x, v.puntos)
        self.assertAlmostEqual(x.longitud, puntos[5].longitud, places=5)
        self.assertAlmostEqual(x.latitud, puntos[5].latitud, places=5)

    def test_K_movimiento_diario_oficial_no_cambia(self):
        puntos = con_salto()
        v = analizar_vehiculo(puntos)
        esperado = distancia_por_dia(v.puntos, calcular_tramos(v.puntos))  # sobre los datos originales
        self.assertEqual(v.actividad.distancia_por_dia, esperado)
        self.assertAlmostEqual(sum(v.actividad.distancia_por_dia.values()),
                               v.resumen.distancia_estimada_observada_km, places=9)
        c = v.calidad_gps
        self.assertAlmostEqual(sum(c.distancia_por_dia_depurada.values()), c.distancia_depurada_km, places=9)
        self.assertLess(sum(c.distancia_por_dia_depurada.values()), sum(esperado.values()))

    def test_L_prioridad_oficial_no_cambia(self):
        v = analizar_vehiculo(con_salto())
        self.assertEqual(v.calidad_gps.estado, CALIDAD_REVISAR)
        con_calidad = evaluar_vehiculo(v)
        sin_calidad = evaluar_vehiculo(dataclasses.replace(v, calidad_gps=None))
        self.assertEqual(con_calidad.prioridad, sin_calidad.prioridad)
        self.assertEqual(con_calidad.nivel_movimiento, sin_calidad.nivel_movimiento)
        self.assertEqual(con_calidad.distancia_observada_km, sin_calidad.distancia_observada_km)
        self.assertEqual(con_calidad.motivos, sin_calidad.motivos)
        self.assertEqual(con_calidad.calidad_gps, CALIDAD_REVISAR)  # solo se muestra como dimensión aparte

    def test_J_salto_que_cruza_otra_entidad_no_cambia_el_geofencing_oficial(self):
        # Ruta dentro de la zona A (lat < 25) y un único punto que cae en "Hidalgo" (lat > 25) y regresa.
        puntos = [Punto(T0 + timedelta(seconds=6 * i), 24.980 + 0.0003 * i, -100.0, 0.0) for i in range(11)]
        x = puntos[5]
        puntos[5] = Punto(x.fecha, 25.05, -100.0, 0.0)
        v = analizar_vehiculo(puntos, ZONAS)
        c = v.calidad_gps
        print(f"\n  [J] saltos={len(c.saltos_probables)} oficial: fuera={v.geofencing.puntos_fuera} "
              f"no_conf={v.geofencing.salidas_no_confirmadas} | experimental: "
              f"fuera={c.geofencing_depurado.puntos_fuera}", end="")
        self.assertEqual(len(c.saltos_probables), 1)  # el diagnóstico lo detecta
        # Oficial intacto: el punto sigue contando como FUERA y genera su evento no confirmado.
        self.assertEqual(v.geofencing.puntos_fuera, 1)
        self.assertEqual(v.geofencing.salidas_no_confirmadas, 1)
        self.assertEqual(v.geofencing.salidas_confirmadas, 0)
        self.assertEqual(v.geofencing.entidades_fuera, ())
        # Comparación EXPERIMENTAL aparte.
        self.assertEqual(c.geofencing_depurado.puntos_fuera, 0)
        self.assertEqual(c.geofencing_depurado.salidas_no_confirmadas, 0)

    # ---------- I, M: sin falsos positivos ----------

    def test_I_jitter_estacionario_no_es_salto(self):
        # Vehículo detenido con ruido de pocos metros cada 1-5 s, incluyendo saltitos de ~15 m.
        patron = [(0.0, 0.0), (0.004, 0.003), (-0.003, 0.002), (0.012, -0.010), (0.0, 0.005), (-0.015, 0.0)]
        puntos, t = [], 0
        for i in range(60):
            n, e = patron[i % len(patron)]
            puntos.append(pto(t, n, e))
            t += 1 + (i % 5)
        c = analizar_calidad_gps(puntos)
        self.mostrar("I", c)
        self.assertEqual(c.estado, CALIDAD_SIN_ALERTAS)
        self.assertEqual(c.saltos_probables, [])
        self.assertEqual(c.anomalias_complejas, [])

    def test_M_alta_frecuencia_coherente_no_genera_saltos(self):
        # 10 min a 80 km/h con un punto por segundo o por 1-3 s irregulares y ruido de ~3 m.
        puntos, t, norte = [], 0, 0.0
        for i in range(500):
            ruido = 0.003 * (1 if i % 2 else -1)
            puntos.append(pto(t, norte, ruido))
            paso = 1 + (i * 7) % 3
            t += paso
            norte += 80 * paso / 3600
        c = analizar_calidad_gps(puntos)
        self.mostrar("M", c)
        self.assertEqual(len(c.saltos_probables), 0)
        self.assertEqual(len(c.anomalias_complejas), 0)
        self.assertEqual(c.estado, CALIDAD_SIN_ALERTAS)

    # ---------- N, O: bordes numéricos ----------

    def test_N_ab_casi_cero_no_divide_por_cero(self):
        # A y B coinciden exactamente; X se aleja 2 km y regresa.
        puntos = [pto(0, 0.0), pto(6, 0.0, 2.0), pto(12, 0.0)]
        e = evaluar_secuencia(puntos, 0, 2)
        self.assertEqual(e.distancia_ab_km, 0.0)
        self.assertTrue(math.isinf(e.relacion_desvio))
        self.assertTrue(e.condiciones["relacion_desvio"])
        self.assertEqual(e.velocidad_ab_kmh, 0.0)
        c = analizar_calidad_gps(puntos)
        self.assertEqual(len(c.saltos_probables), 1)
        s = c.saltos_probables[0]
        self.assertTrue(math.isinf(s.relacion_desvio))
        self.assertAlmostEqual(c.distancia_depurada_km, 0.0, places=9)  # oficial - AX - XB + 0

    def test_N2_ab_y_recorrido_cero(self):
        puntos = [pto(0, 0.0), pto(6, 0.0), pto(12, 0.0)]
        e = evaluar_secuencia(puntos, 0, 2)
        self.assertEqual(e.relacion_desvio, 0.0)
        self.assertFalse(e.cumple)

    def test_O_timestamp_con_delta_cero_no_rompe_ni_produce_velocidad_infinita(self):
        a = pto(0, 0.0)
        b = Punto(a.fecha, a.latitud + 2 * KM_LAT, a.longitud, 0.0)  # mismo instante, 2 km
        puntos = [a, b, pto(10, 0.05), pto(20, 0.1), pto(30, 0.15)]
        c = analizar_calidad_gps(puntos)  # sin pasar por la consolidación: delta 0 a propósito
        self.mostrar("O", c)
        self.assertTrue(math.isfinite(c.velocidad_maxima_kmh))
        self.assertTrue(all(math.isfinite(t.velocidad_estimada_kmh) for t in c.tramos_velocidad_sospechosa))
        e = evaluar_secuencia(puntos, 0, 2)
        self.assertIsNone(e.velocidad_ax_kmh)  # sin duración: no hay velocidad
        self.assertFalse(e.cumple)
        self.assertEqual(c.saltos_probables, [])


if __name__ == "__main__":
    unittest.main()
