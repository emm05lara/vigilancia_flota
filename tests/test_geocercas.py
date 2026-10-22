"""Pruebas de geocercas con geometrías artificiales (sin internet ni archivos INEGI).

Mapa artificial (lon/lat reales, cerca de la CDMX):

    lat 19.60 +-----------+-----------+
              |  Hidalgo  |  Hidalgo  |    (HID: lon -99.30..-98.90)
    lat 19.40 +-----------+-----------+-----------+
              |   ZONA A  |   ZONA B  |  Puebla   |
    lat 19.20 +-----------+-----------+-----------+
           -99.30      -99.10      -98.90      -98.70

Zona permitida = A (clave "A") + B (clave "B"). Frontera norte de la zona: lat 19.40.

Ejecutar desde la raíz del proyecto:
    py -m unittest tests.test_geocercas -v
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import box

from src.analizador import analizar_carpeta
from src.geocercas import asociar_con_trayectos, resumir_geofencing
from src.gpx_parser import ZONA_HORARIA, Punto
from src.movimiento import resumir_actividad
from src.zonas import DENTRO, FUERA, TOLERANCIA, Entidad, Zonas, clasificar_punto

INICIO = datetime(2026, 10, 1, 10, 0, 0, tzinfo=ZONA_HORARIA)

ZONAS = Zonas(
    entidades=[
        Entidad("A", "Zona A", box(-99.30, 19.20, -99.10, 19.40)),
        Entidad("B", "Zona B", box(-99.10, 19.20, -98.90, 19.40)),
        Entidad("HID", "Hidalgo", box(-99.30, 19.40, -98.90, 19.60)),
        Entidad("PUE", "Puebla", box(-98.90, 19.20, -98.70, 19.60)),
    ],
    claves_permitidas=("A", "B"),
    tolerancia_metros=100,
)

# Posiciones tipo (lat, lon)
DENTRO_A = (19.30, -99.20)
DENTRO_B = (19.30, -99.00)
HIDALGO = (19.50, -99.20)
PUEBLA = (19.30, -98.80)
APENAS_FUERA = (19.4005, -99.20)  # ~55 m al norte de la frontera, en Hidalgo
CLARAMENTE_FUERA = (19.45, -99.20)  # ~5.5 km


def serie(*posiciones, paso_min: float = 5) -> list[Punto]:
    """Puntos cada paso_min minutos con las posiciones dadas, en orden."""
    return [Punto(INICIO + timedelta(minutes=paso_min * i), lat, lon, 0.0)
            for i, (lat, lon) in enumerate(posiciones)]


def con_horas(*datos) -> list[Punto]:
    """datos: (minutos desde INICIO, (lat, lon))."""
    return [Punto(INICIO + timedelta(minutes=m), lat, lon, 0.0) for m, (lat, lon) in datos]


def escribir_gpx(carpeta: Path, nombre: str, puntos: list[Punto]) -> None:
    cuerpo = "".join(
        f'<trkpt lat="{p.latitud:.6f}" lon="{p.longitud:.6f}"><ele>0.0</ele>'
        f'<time>{p.fecha.strftime("%Y-%m-%d %H:%M:%S")}</time></trkpt>' for p in puntos)
    (carpeta / nombre).write_text(
        f'<?xml version="1.0"?><gpx><trk><trkseg>{cuerpo}</trkseg></trk></gpx>', encoding="utf-8")


class PruebasGeocercas(unittest.TestCase):
    def resumen(self, puntos, **kwargs):
        return resumir_geofencing(puntos, ZONAS, **kwargs)

    def mostrar(self, caso, r):
        print(f"\n  [{caso}] dentro={r.puntos_dentro} tol={r.puntos_tolerancia} fuera={r.puntos_fuera} "
              f"confirmadas={r.salidas_confirmadas} no_conf={r.salidas_no_confirmadas} "
              f"inicia_fuera={r.inicia_fuera} termino_fuera={r.termino_fuera} entidades={list(r.entidades_fuera)}",
              end="")

    # ---------- clasificación de puntos ----------

    def test_F_apenas_fuera_es_tolerancia(self):
        c = clasificar_punto(serie(APENAS_FUERA)[0], ZONAS)
        print(f"\n  [F] estado={c.estado} distancia={c.distancia_a_zona_m:.0f} m entidad={c.entidad}", end="")
        self.assertEqual(c.estado, TOLERANCIA)
        self.assertLessEqual(c.distancia_a_zona_m, 100)
        self.assertFalse(c.dentro_zona_permitida)

    def test_G_claramente_fuera(self):
        c = clasificar_punto(serie(CLARAMENTE_FUERA)[0], ZONAS)
        print(f"\n  [G] estado={c.estado} distancia={c.distancia_a_zona_m:.0f} m entidad={c.entidad}", end="")
        self.assertEqual(c.estado, FUERA)
        self.assertEqual(c.entidad, "Hidalgo")
        self.assertEqual(c.clave_entidad, "HID")

    def test_dentro_detecta_la_entidad_permitida(self):
        a = clasificar_punto(serie(DENTRO_A)[0], ZONAS)
        b = clasificar_punto(serie(DENTRO_B)[0], ZONAS)
        self.assertEqual((a.estado, a.entidad), (DENTRO, "Zona A"))
        self.assertEqual((b.estado, b.entidad), (DENTRO, "Zona B"))
        self.assertIsNone(a.distancia_a_zona_m)

    def test_frontera_interna_entre_entidades_permitidas_no_cuenta(self):
        # Cruzar de A a B es movimiento dentro de la zona permitida.
        r = self.resumen(serie(DENTRO_A, DENTRO_B, DENTRO_A))
        self.assertEqual(r.puntos_dentro, 3)
        self.assertEqual(r.eventos, [])

    def test_punto_sin_entidad_queda_fuera_con_nombre_generico(self):
        lejos = (25.0, -110.0)
        r = self.resumen(serie(DENTRO_A, lejos, lejos, DENTRO_A))
        self.assertEqual(r.salidas_confirmadas, 1)
        self.assertEqual(r.eventos[0].entidades, ("Sin entidad identificada",))

    # ---------- eventos ----------

    def test_A_todos_dentro(self):
        r = self.resumen(serie(DENTRO_A, DENTRO_A, DENTRO_B, DENTRO_B))
        self.mostrar("A", r)
        self.assertEqual(r.salidas_confirmadas, 0)
        self.assertEqual(r.salidas_no_confirmadas, 0)
        self.assertFalse(r.tuvo_salida_confirmada)
        self.assertFalse(r.termino_fuera)

    def test_B_un_solo_punto_fuera_no_confirma(self):
        r = self.resumen(serie(DENTRO_A, HIDALGO, DENTRO_A))
        self.mostrar("B", r)
        self.assertEqual(r.salidas_confirmadas, 0)
        self.assertEqual(r.salidas_no_confirmadas, 1)
        self.assertFalse(r.eventos[0].confirmado)
        self.assertEqual(r.entidades_fuera, ())  # un evento no confirmado no reporta entidades

    def test_C_dos_puntos_fuera_y_regreso_confirma(self):
        r = self.resumen(serie(DENTRO_A, DENTRO_B, HIDALGO, HIDALGO, HIDALGO, DENTRO_B))
        self.mostrar("C", r)
        self.assertEqual(r.salidas_confirmadas, 1)  # un evento, no uno por punto
        e = r.eventos[0]
        self.assertTrue(e.confirmado)
        self.assertFalse(e.abierta)
        self.assertEqual(e.cantidad_puntos_fuera, 3)
        self.assertEqual(e.ultimo_punto_dentro.punto.fecha, INICIO + timedelta(minutes=5))
        self.assertEqual(e.primer_punto_fuera.punto.fecha, INICIO + timedelta(minutes=10))
        self.assertEqual(e.ultimo_punto_fuera.punto.fecha, INICIO + timedelta(minutes=20))
        self.assertEqual(e.primer_punto_dentro_posterior.punto.fecha, INICIO + timedelta(minutes=25))
        self.assertEqual(e.incertidumbre_salida, timedelta(minutes=5))
        self.assertEqual(e.incertidumbre_regreso, timedelta(minutes=5))
        self.assertEqual(e.duracion_observable_fuera, timedelta(minutes=10))
        self.assertEqual(e.duracion_maxima_posible, timedelta(minutes=20))

    def test_D_termina_fuera_salida_abierta(self):
        r = self.resumen(serie(DENTRO_A, HIDALGO, HIDALGO))
        self.mostrar("D", r)
        e = r.eventos[0]
        self.assertEqual(r.salidas_confirmadas, 1)
        self.assertTrue(e.confirmado and e.abierta)
        self.assertIsNone(e.primer_punto_dentro_posterior)  # no se inventa un regreso
        self.assertIsNone(e.duracion_maxima_posible)
        self.assertIsNone(e.incertidumbre_regreso)
        self.assertTrue(r.termino_fuera)

    def test_E_gpx_empieza_fuera(self):
        r = self.resumen(serie(HIDALGO, HIDALGO, DENTRO_A, DENTRO_A))
        self.mostrar("E", r)
        e = r.eventos[0]
        self.assertTrue(e.inicia_fuera)
        self.assertIsNone(e.ultimo_punto_dentro)  # no se inventa una salida previa
        self.assertIsNone(e.incertidumbre_salida)
        self.assertEqual(r.salidas_confirmadas, 0)  # no se observó ninguna salida
        self.assertTrue(r.inicia_fuera)
        self.assertFalse(r.termino_fuera)

    def test_H_salida_a_hidalgo_y_regreso(self):
        r = self.resumen(serie(DENTRO_A, HIDALGO, HIDALGO, DENTRO_A))
        self.mostrar("H", r)
        self.assertEqual(r.entidades_fuera, ("Hidalgo",))
        self.assertEqual(r.eventos[0].claves_entidades, ("HID",))

    def test_I_hidalgo_y_puebla_en_un_solo_evento(self):
        # Fuera todo el tiempo: Hidalgo -> Puebla sin pasar por la zona permitida.
        pasa_por_puebla = (19.45, -98.85)
        r = self.resumen(serie(DENTRO_A, HIDALGO, HIDALGO, pasa_por_puebla, PUEBLA, DENTRO_B))
        self.mostrar("I", r)
        self.assertEqual(r.salidas_confirmadas, 1)
        self.assertEqual(len(r.eventos), 1)
        self.assertEqual(r.eventos[0].entidades, ("Hidalgo", "Puebla"))
        self.assertEqual(r.eventos[0].cantidad_puntos_fuera, 4)

    # ---------- tolerancia ----------

    def test_tolerancia_no_abre_ni_confirma_salida(self):
        r = self.resumen(serie(DENTRO_A, APENAS_FUERA, APENAS_FUERA, APENAS_FUERA, DENTRO_A))
        self.mostrar("tol", r)
        self.assertEqual(r.puntos_tolerancia, 3)
        self.assertEqual(r.puntos_fuera, 0)
        self.assertEqual(r.eventos, [])

    def test_tolerancia_no_cuenta_como_punto_fuera_confirmado(self):
        # FUERA, TOLERANCIA, FUERA: nunca hay 2 FUERA consecutivos.
        r = self.resumen(serie(DENTRO_A, CLARAMENTE_FUERA, APENAS_FUERA, CLARAMENTE_FUERA, DENTRO_A))
        self.mostrar("tol2", r)
        self.assertEqual(r.salidas_confirmadas, 0)
        self.assertEqual(r.salidas_no_confirmadas, 1)
        self.assertEqual(r.eventos[0].cantidad_puntos_tolerancia, 1)  # conservado para diagnóstico
        self.assertEqual(len(r.eventos[0].puntos_periodo), 3)

    def test_tolerancia_antes_de_salida_confirmada(self):
        r = self.resumen(serie(DENTRO_A, APENAS_FUERA, CLARAMENTE_FUERA, CLARAMENTE_FUERA, DENTRO_A))
        self.assertEqual(r.salidas_confirmadas, 1)
        self.assertEqual(r.eventos[0].cantidad_puntos_tolerancia, 1)

    # ---------- huecos con desplazamiento ----------

    def test_J_hueco_de_dentro_a_fuera(self):
        puntos = con_horas((0, DENTRO_A), (120, HIDALGO), (125, HIDALGO), (130, DENTRO_A))
        r = self.resumen(puntos)
        self.mostrar("J", r)
        e = r.eventos[0]
        self.assertTrue(e.salida_con_hueco)  # información incompleta, no hay cruce que reportar
        self.assertFalse(e.regreso_con_hueco)
        self.assertEqual(e.ultimo_punto_dentro.punto.fecha, INICIO)
        self.assertEqual(e.primer_punto_fuera.punto.fecha, INICIO + timedelta(minutes=120))
        self.assertEqual(e.incertidumbre_salida, timedelta(hours=2))  # ventana, no hora de cruce
        self.assertEqual(e.duracion_observable_fuera, timedelta(minutes=5))

    def test_K_hueco_de_fuera_a_dentro(self):
        puntos = con_horas((0, DENTRO_A), (5, HIDALGO), (10, HIDALGO), (130, DENTRO_A))
        r = self.resumen(puntos)
        self.mostrar("K", r)
        e = r.eventos[0]
        self.assertTrue(e.regreso_con_hueco)
        self.assertFalse(e.salida_con_hueco)
        self.assertEqual(e.incertidumbre_regreso, timedelta(minutes=120))
        self.assertEqual(e.duracion_observable_fuera, timedelta(minutes=5))  # no se estira hasta el regreso
        self.assertEqual(e.duracion_maxima_posible, timedelta(minutes=130))

    def test_parada_larga_junto_a_la_frontera_no_genera_salida(self):
        # 3 horas sin puntos entre un punto DENTRO y uno en TOLERANCIA a ~44 m:
        # parada probable (no hueco con desplazamiento) y sin evento de salida.
        puntos = con_horas((0, DENTRO_A), (5, (19.3999, -99.20)), (185, (19.4004, -99.20)),
                           (190, (19.4004, -99.20)))
        r = self.resumen(puntos)
        self.assertEqual(r.puntos_tolerancia, 2)
        self.assertEqual(r.eventos, [])

    # ---------- consolidación y trayectos ----------

    def test_L_gpx_solapados_no_duplican_eventos(self):
        puntos = serie(DENTRO_A, HIDALGO, HIDALGO, DENTRO_A, DENTRO_A)
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp) / "veh"
            carpeta.mkdir()
            escribir_gpx(carpeta, "a.gpx", puntos)
            escribir_gpx(carpeta, "b.gpx", puntos)  # idéntico
            escribir_gpx(carpeta, "c.gpx", puntos[2:])  # solapado parcialmente
            v = analizar_carpeta(carpeta, {}, ZONAS)
        r = v.geofencing
        self.mostrar("L", r)
        self.assertEqual(len(v.puntos), 5)
        self.assertEqual(r.salidas_confirmadas, 1)
        self.assertEqual(len(r.eventos), 1)
        self.assertEqual(r.puntos_fuera, 2)

    def test_asociacion_con_trayectos(self):
        # Trayecto de ~11 km hacia el norte que sale a Hidalgo (cada paso 0.02 grados ~ 2.2 km).
        lat = [19.30 + 0.02 * i for i in range(8)]  # 19.30 ... 19.44
        puntos = serie(*[(la, -99.20) for la in lat])
        r = self.resumen(puntos)
        trayectos = resumir_actividad(puntos).trayectos
        asociados = asociar_con_trayectos(r.eventos, trayectos)
        print(f"\n  [trayectos] trayectos={len(trayectos)} asociados={ {k: [e.entidades for e in v] for k, v in asociados.items()} }", end="")
        self.assertEqual(len(trayectos), 1)
        self.assertIn(1, asociados)
        self.assertEqual(asociados[1][0].entidades, ("Hidalgo",))


if __name__ == "__main__":
    unittest.main()
