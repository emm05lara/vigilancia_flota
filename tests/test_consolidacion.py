"""Pruebas controladas de consolidación de GPX y separación de distancias.

Ejecutar desde la raíz del proyecto:
    py -m unittest tests.test_consolidacion -v
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.analizador import analizar_carpeta
from src.gpx_parser import ZONA_HORARIA, Punto
from src.metricas import consolidar_puntos, haversine_km

INICIO = datetime(2026, 10, 1, 8, 0, 0, tzinfo=ZONA_HORARIA)
LAT0, LON0 = 19.30, -99.20
PASO_LAT = 0.01  # ~1.1 km hacia el norte cada 5 min


def punto(minutos: float, lat: float, lon: float = LON0) -> Punto:
    return Punto(INICIO + timedelta(minutes=minutos), lat, lon, 0.0)


def ruta(indices, desplazamiento_min: float = 0, lat_extra: float = 0.0) -> list[Punto]:
    """Puntos cada 5 min; el índice i está en lat LAT0 + i*PASO_LAT."""
    return [punto(i * 5 + desplazamiento_min, LAT0 + i * PASO_LAT + lat_extra) for i in indices]


def escribir_gpx(carpeta: Path, nombre: str, puntos: list[Punto]) -> None:
    cuerpo = "".join(
        f'<trkpt lat="{p.latitud:.6f}" lon="{p.longitud:.6f}"><ele>0.0</ele>'
        f'<time>{p.fecha.strftime("%Y-%m-%d %H:%M:%S")}</time></trkpt>'
        for p in puntos
    )
    (carpeta / nombre).write_text(
        f'<?xml version="1.0"?><gpx><trk><trkseg>{cuerpo}</trkseg></trk></gpx>', encoding="utf-8")


class PruebasConsolidacion(unittest.TestCase):
    def analizar(self, **archivos):
        """Crea una carpeta temporal con los GPX dados (nombre=lista de puntos) y la analiza."""
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp) / "vehiculo"
            carpeta.mkdir()
            for nombre, puntos in archivos.items():
                escribir_gpx(carpeta, f"{nombre}.gpx", puntos)
            return analizar_carpeta(carpeta, {})

    def mostrar(self, caso, v):
        r, a = v.resumen, v.actividad
        print(f"\n  [{caso}] puntos={r.cantidad_puntos} dup={v.duplicados_eliminados} "
              f"conflictos={len(v.conflictos_timestamp)} obs={r.distancia_estimada_observada_km:.2f} km "
              f"ruido={r.distancia_ruido_paradas_km:.3f} km huecos={r.desplazamiento_minimo_huecos_km:.2f} km "
              f"paradas={a.cantidad_paradas_probables} h.desp={a.cantidad_huecos_con_desplazamiento} "
              f"trayectos={len(a.trayectos)}", end="")

    # Referencia: un solo archivo con los 10 puntos completos.
    def referencia(self):
        return self.analizar(completo=ruta(range(10))).resumen

    def test_A_dos_gpx_completamente_solapados(self):
        v = self.analizar(a=ruta(range(10)), b=ruta(range(10)))
        self.mostrar("A", v)
        ref = self.referencia()
        self.assertEqual(v.resumen.cantidad_puntos, 10)
        self.assertEqual(v.duplicados_eliminados, 10)
        self.assertEqual(v.conflictos_timestamp, [])
        self.assertAlmostEqual(v.resumen.distancia_estimada_observada_km, ref.distancia_estimada_observada_km)

    def test_B_dos_gpx_parcialmente_solapados(self):
        v = self.analizar(a=ruta(range(0, 7)), b=ruta(range(4, 10)))  # comunes: 4, 5, 6
        self.mostrar("B", v)
        ref = self.referencia()
        self.assertEqual(v.resumen.cantidad_puntos, 10)
        self.assertEqual(v.duplicados_eliminados, 3)
        self.assertAlmostEqual(v.resumen.distancia_estimada_observada_km, ref.distancia_estimada_observada_km)
        self.assertEqual(len(v.actividad.trayectos), 1)

    def test_C_mismo_timestamp_coordenadas_casi_iguales(self):
        p1 = punto(0, 19.300000)
        p2 = punto(0, 19.300030)  # ~3.3 m
        c = consolidar_puntos([p1, p2])
        print(f"\n  [C] puntos={len(c.puntos)} duplicados={c.duplicados_eliminados} conflictos={len(c.conflictos)}", end="")
        self.assertEqual(len(c.puntos), 1)
        self.assertEqual(c.duplicados_eliminados, 1)
        self.assertEqual(c.conflictos, [])

    def test_D_mismo_timestamp_separados_significativamente(self):
        p1 = punto(0, 19.300000)
        p2 = punto(0, 19.310000)  # ~1.1 km
        c = consolidar_puntos([p1, p2])
        print(f"\n  [D] puntos={len(c.puntos)} duplicados={c.duplicados_eliminados} conflictos={len(c.conflictos)} "
              f"({c.conflictos[0].distancia_m:.0f} m)", end="")
        self.assertEqual(c.duplicados_eliminados, 0)
        self.assertEqual(len(c.conflictos), 1)
        self.assertGreater(c.conflictos[0].distancia_m, 1000)
        self.assertEqual(c.conflictos[0].coordenada_conservada, (19.30, LON0))
        self.assertEqual(c.conflictos[0].coordenada_excluida, (19.31, LON0))

    def test_D2_conflicto_no_detiene_el_analisis(self):
        con_conflicto = ruta(range(10)) + [punto(10, LAT0 + 0.5)]  # mismo timestamp que el índice 2
        con_conflicto[-1] = Punto(ruta([2])[0].fecha, LAT0 + 0.5, LON0, 0.0)
        v = self.analizar(a=con_conflicto)
        self.mostrar("D2", v)
        self.assertEqual(len(v.conflictos_timestamp), 1)
        self.assertEqual(v.resumen.cantidad_puntos, 10)
        self.assertAlmostEqual(v.resumen.distancia_estimada_observada_km, self.referencia().distancia_estimada_observada_km)

    def test_E_dos_gpx_consecutivos_intervalo_normal(self):
        v = self.analizar(a=ruta(range(0, 5)), b=ruta(range(5, 10)))  # 5 min entre archivos
        self.mostrar("E", v)
        ref = self.referencia()
        self.assertEqual(v.actividad.cantidad_paradas_probables, 0)
        self.assertEqual(v.actividad.cantidad_huecos_con_desplazamiento, 0)
        self.assertEqual(len(v.actividad.trayectos), 1)
        self.assertAlmostEqual(v.resumen.distancia_estimada_observada_km, ref.distancia_estimada_observada_km)

    def test_F_separados_horas_mismo_lugar(self):
        # B empieza 8 h después, a ~5 m de donde terminó A, y sigue avanzando.
        a = ruta(range(0, 5))
        b = [punto(8 * 60 + i * 5, LAT0 + 4 * PASO_LAT + 0.00005 + i * PASO_LAT) for i in range(5)]
        v = self.analizar(a=a, b=b)
        self.mostrar("F", v)
        r = v.resumen
        interna = 4 * haversine_km(LAT0, LON0, LAT0 + PASO_LAT, LON0)
        self.assertEqual(v.actividad.cantidad_paradas_probables, 1)
        self.assertEqual(v.actividad.cantidad_huecos_con_desplazamiento, 0)
        self.assertAlmostEqual(r.distancia_estimada_observada_km, 2 * interna, places=2)
        self.assertLess(r.distancia_ruido_paradas_km, 0.01)  # ruido, no distancia real
        self.assertEqual(r.desplazamiento_minimo_huecos_km, 0)
        self.assertEqual(len(v.actividad.trayectos), 2)

    def test_G_separados_horas_extremos_lejos(self):
        a = ruta(range(0, 5))
        b = [punto(8 * 60 + i * 5, LAT0 + 0.16 + i * PASO_LAT) for i in range(5)]  # ~13.3 km al norte del fin de A
        v = self.analizar(a=a, b=b)
        self.mostrar("G", v)
        r = v.resumen
        ultimo_a, primero_b = a[-1], b[0]
        recta = haversine_km(ultimo_a.latitud, ultimo_a.longitud, primero_b.latitud, primero_b.longitud)
        interna = 4 * haversine_km(LAT0, LON0, LAT0 + PASO_LAT, LON0)
        self.assertEqual(v.actividad.cantidad_huecos_con_desplazamiento, 1)
        self.assertEqual(v.actividad.cantidad_paradas_probables, 0)
        self.assertAlmostEqual(r.desplazamiento_minimo_huecos_km, recta, places=3)
        self.assertGreater(recta, 10)
        self.assertAlmostEqual(r.distancia_estimada_observada_km, 2 * interna, places=2)  # sin el puente
        self.assertEqual(len(v.actividad.trayectos), 2)
        self.assertAlmostEqual(r.distancia_bruta_km, 2 * interna + recta, places=2)

    def test_G2_hueco_dentro_de_un_solo_gpx(self):
        # La regla depende de datos y tiempos, no del archivo: mismo resultado en un único GPX.
        a = ruta(range(0, 5))
        b = [punto(8 * 60 + i * 5, LAT0 + 0.16 + i * PASO_LAT) for i in range(5)]
        separados = self.analizar(a=a, b=b).resumen
        unico = self.analizar(todo=a + b).resumen
        self.assertAlmostEqual(unico.distancia_estimada_observada_km, separados.distancia_estimada_observada_km)
        self.assertAlmostEqual(unico.desplazamiento_minimo_huecos_km, separados.desplazamiento_minimo_huecos_km)


if __name__ == "__main__":
    unittest.main()
