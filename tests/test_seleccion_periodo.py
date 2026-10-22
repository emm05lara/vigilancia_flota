"""Pruebas del análisis local por subperiodo (etapa 8). Sin red, sin Streamlit, con fixtures temporales.

    py -m unittest tests.test_seleccion_periodo -v
"""

import csv
import hashlib
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

import requests

import src.integracion_periodo as ip
import src.seleccion_periodo as sp
from crear_carpetas_llaves import limpiar_nombre_carpeta
from src.analizador import analizar_archivos
from src.descarga_lote import nombre_archivo_lote
from src.gpx_parser import ZONA_HORARIA, Punto, filtrar_ventana
from src.manifiesto import nombre_manifiesto
from src.metricas import haversine_km
from src.prioridades import (DATOS_COBERTURA_LOCAL_INSUFICIENTE, DATOS_ERROR_ADQUISICION, DATOS_OK,
                             DATOS_SIN_DATOS_PERIODO, DATOS_SIN_DISPOSITIVO, DATOS_SIN_GPX,
                             DATOS_SIN_POSICIONES_SUBPERIODO, filtrar_evaluaciones, generar_csv, nivel_movimiento,
                             resumir_flota)
from tests.test_mapeo_dno import crear_excel
from tests.test_prioridades import PASO, ZONAS

SEM_INI, SEM_FIN = datetime(2026, 10, 12, 0, 0, 0), datetime(2026, 10, 18, 23, 59, 59)
DOS_INI, DOS_FIN = datetime(2026, 10, 15, 0, 0, 0), datetime(2026, 10, 16, 23, 59, 59)


def llave(n):
    return f"AVEO|2022|AZUL|PLACA{n:02d}"


def viaje(dia: date, k: int, lat0: float = 19.0, hora: int = 8, lon: float = -100.0):
    """Sale al norte k pasos de ~5 km (5 min c/u) y regresa: 2k tramos normales."""
    inicio = datetime.combine(dia, time(hora))
    return [(inicio + timedelta(minutes=5 * i), lat0 + PASO * (i if i <= k else 2 * k - i), lon)
            for i in range(2 * k + 1)]


def semana(lat0=19.0, dias=range(12, 19), extra=None):
    """Un viaje por día; el día d tiene k = d - 10 pasos, así cada día tiene una distancia distinta."""
    pts = []
    for d in dias:
        pts += viaje(date(2026, 10, d), d - 10, lat0)
    return pts + list(extra or [])


def distancia(pts):
    """Distancia esperada, calculada de forma independiente (todos los tramos de estas fixtures son normales)."""
    pts = sorted(pts)
    return sum(haversine_km(a[1], a[2], b[1], b[2]) for a, b in zip(pts, pts[1:]))


def gpx_bytes(pts) -> bytes:
    cuerpo = "".join(f'<trkpt lat="{la}" lon="{lo}"><ele>0.0</ele><time>{t:%Y-%m-%d %H:%M:%S}</time></trkpt>\n'
                     for t, la, lo in pts)
    return f'<?xml version="1.0"?><gpx><trk><trkseg>\n{cuerpo}</trkseg></trk></gpx>'.encode()


def entrada(llave_, estado, archivo=None, actual=None):
    return {"llave": llave_, "estado_adquisicion": estado, "estado_actual_baanool": actual,
            "archivo_gpx": archivo, "cantidad_puntos": None, "mensaje": "detalle" if estado.startswith("ERROR") else None,
            "ultima_actualizacion": "2026-10-19T12:00:00-06:00"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.entrada = self.raiz / "entrada"
        self.entrada.mkdir()
        self.manifiestos = self.raiz / "salida" / "estado_descargas"

    def tearDown(self):
        self._tmp.cleanup()

    def poner_gpx(self, n, ini, fin, pts):
        carpeta = self.entrada / limpiar_nombre_carpeta(llave(n))
        carpeta.mkdir(exist_ok=True)
        nombre = nombre_archivo_lote(ini, fin)
        (carpeta / nombre).write_bytes(gpx_bytes(pts))
        return f"entrada/{carpeta.name}/{nombre}"

    def poner_manifiesto(self, ini, fin, entradas, ejecucion="2026-10-19T12:00:00-06:00", nombre=None, **extra):
        datos = {"version_formato": 1, "periodo_inicio_local": ini.strftime("%Y-%m-%d %H:%M:%S"),
                 "periodo_fin_local": fin.strftime("%Y-%m-%d %H:%M:%S"), "zona_horaria_local": "America/Mexico_City",
                 "fecha_ejecucion": ejecucion, "lote_completo": True, "motivo_interrupcion": None, "nota": "",
                 "resumen": {}, "vehiculos": list(entradas)}
        datos.update(extra)
        self.manifiestos.mkdir(parents=True, exist_ok=True)
        ruta = self.manifiestos / (nombre or nombre_manifiesto(ini, fin))
        ruta.write_text(json.dumps(datos), encoding="utf-8")
        return ruta

    def adquirir(self, ini, fin, pts_por_llave, **kw):
        """Escribe GPX + manifiesto como lo haría una adquisición. pts_por_llave: {n: puntos}."""
        entradas = []
        for n, pts in pts_por_llave.items():
            ruta = self.poner_gpx(n, ini, fin, pts)
            entradas.append(entrada(llave(n), "DESCARGADO", ruta, "ONLINE"))
        return self.poner_manifiesto(ini, fin, entradas, **kw)

    def excel(self, n_llaves, extra=()):
        return crear_excel(self.raiz, [(llave(i), None) for i in range(n_llaves)] + [(l, None) for l in extra],
                           con_dno=False)

    def analizar(self, ini, fin, n_llaves=1, extra=(), zonas=ZONAS):
        return sp.analizar_periodo_local(ini, fin, self.excel(n_llaves, extra), self.raiz, zonas, self.manifiestos)

    @staticmethod
    def ev(res, n):
        return next(e for e in res.evaluaciones if e.identificador == llave(n))

    def huella(self):
        """{ruta relativa: sha256} de TODOS los archivos bajo la raíz de la fixture."""
        return {p.relative_to(self.raiz).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.raiz.rglob("*")) if p.is_file()}


# ---------- selección de fuentes ----------

class PruebasSeleccion(Base):
    def test_A_manifiesto_exacto_con_gpx_valido_se_utiliza(self):
        self.adquirir(DOS_INI, DOS_FIN, {0: viaje(date(2026, 10, 15), 3) + viaje(date(2026, 10, 16), 4)})
        r = self.analizar(DOS_INI, DOS_FIN)
        f = r.fuentes[llave(0)]
        self.assertEqual((f.estado_datos, f.tipo), (sp.ANALIZABLE, sp.TIPO_EXACTA))
        self.assertEqual(self.ev(r, 0).estado_datos, DATOS_OK)
        self.assertFalse(r.usa_adquisicion_mayor)

    def test_B_manifiesto_semanal_que_contiene_dos_dias_se_utiliza(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        r = self.analizar(DOS_INI, DOS_FIN)
        f = r.fuentes[llave(0)]
        self.assertEqual(f.tipo, sp.TIPO_MAYOR)
        self.assertEqual((f.adquisicion_inicio, f.adquisicion_fin), (SEM_INI, SEM_FIN))
        self.assertTrue(r.usa_adquisicion_mayor)
        self.assertEqual(set(self.ev(r, 0).distancia_por_dia), {date(2026, 10, 15), date(2026, 10, 16)})

    def test_C_periodo_que_empieza_antes_de_la_adquisicion_no_tiene_cobertura(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        r = self.analizar(datetime(2026, 10, 11, 12, 0, 0), DOS_FIN)
        self.assertTrue(r.sin_cobertura)
        e = self.ev(r, 0)
        self.assertEqual(e.estado_datos, DATOS_COBERTURA_LOCAL_INSUFICIENTE)
        self.assertIsNone(e.prioridad)
        self.assertEqual(r.intervalos_disponibles(), [sp.texto_periodo(SEM_INI, SEM_FIN)])

    def test_D_periodo_que_termina_despues_de_la_adquisicion_no_tiene_cobertura(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        r = self.analizar(DOS_INI, datetime(2026, 10, 19, 0, 0, 1))
        self.assertTrue(r.sin_cobertura)
        self.assertEqual(self.ev(r, 0).estado_datos, DATOS_COBERTURA_LOCAL_INSUFICIENTE)

    def test_D2_fuentes_parciales_no_se_unen(self):
        # 12-15 y 15-18 no cubren 14-16 por separado; no se combinan para fingir cobertura.
        self.adquirir(SEM_INI, datetime(2026, 10, 15, 23, 59, 59), {0: semana(dias=range(12, 16))})
        self.adquirir(datetime(2026, 10, 15, 0, 0, 0), SEM_FIN, {0: semana(dias=range(15, 19))})
        r = self.analizar(datetime(2026, 10, 14, 0, 0, 0), DOS_FIN)
        self.assertTrue(r.sin_cobertura)
        self.assertEqual(self.ev(r, 0).estado_datos, DATOS_COBERTURA_LOCAL_INSUFICIENTE)

    def test_E_dos_fuentes_validas_seleccion_determinista_la_de_menor_duracion(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        corta_ini, corta_fin = datetime(2026, 10, 14, 0, 0, 0), datetime(2026, 10, 17, 23, 59, 59)
        self.adquirir(corta_ini, corta_fin, {0: semana(dias=range(14, 18))})
        elegidos = {self.analizar(DOS_INI, DOS_FIN).fuentes[llave(0)].adquisicion_inicio for _ in range(3)}
        self.assertEqual(elegidos, {corta_ini})

    def test_E2_empate_total_elige_la_ejecucion_mas_reciente_con_advertencia(self):
        pts = semana()
        self.adquirir(SEM_INI, SEM_FIN, {0: pts}, ejecucion="2026-10-19T09:00:00-06:00",
                      nombre="copia_antigua.json")
        self.adquirir(SEM_INI, SEM_FIN, {0: pts}, ejecucion="2026-10-20T09:00:00-06:00")
        r = self.analizar(DOS_INI, DOS_FIN)
        f = r.fuentes[llave(0)]
        self.assertTrue(f.manifiesto.endswith(nombre_manifiesto(SEM_INI, SEM_FIN)))
        self.assertTrue(any("equivalentes" in a for a in f.advertencias))
        self.assertEqual(len(r.avisos_de_seleccion()), 1)

    def test_F_con_exacta_y_semanal_se_prefiere_la_exacta(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        self.adquirir(DOS_INI, DOS_FIN, {0: viaje(date(2026, 10, 15), 3) + viaje(date(2026, 10, 16), 4)})
        r = self.analizar(DOS_INI, DOS_FIN)
        self.assertEqual(r.fuentes[llave(0)].tipo, sp.TIPO_EXACTA)
        self.assertEqual([o["tipo"] for o in r.resumen_origen()], [sp.TIPO_EXACTA])

    def test_G_exacta_sin_archivo_valido_y_semanal_valida_se_usa_la_semanal_con_traza(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        self.poner_manifiesto(DOS_INI, DOS_FIN, [entrada(llave(0), "ERROR_DESCARGA")])
        r = self.analizar(DOS_INI, DOS_FIN)
        f = r.fuentes[llave(0)]
        self.assertEqual(f.tipo, sp.TIPO_MAYOR)
        self.assertEqual(self.ev(r, 0).estado_datos, DATOS_OK)
        self.assertEqual(len(f.advertencias), 1)
        self.assertIn("No se usó la adquisición", f.advertencias[0])
        self.assertIn("ERROR_DESCARGA", f.advertencias[0])

    def test_H_archivo_referido_inexistente_no_inventa_datos(self):
        ruta = self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        (self.entrada / limpiar_nombre_carpeta(llave(0)) / nombre_archivo_lote(SEM_INI, SEM_FIN)).unlink()
        r = self.analizar(DOS_INI, DOS_FIN)
        e = self.ev(r, 0)
        self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
        self.assertIsNone(e.prioridad)
        self.assertEqual(e.resultado.puntos, [])
        self.assertIn("ya no existe", e.inconsistencia)

    def test_I_manifiesto_con_gpx_de_otro_vehiculo_no_se_utiliza(self):
        ruta_otro = self.poner_gpx(1, SEM_INI, SEM_FIN, semana(lat0=21.0))
        self.poner_manifiesto(SEM_INI, SEM_FIN, [entrada(llave(0), "DESCARGADO", ruta_otro)])
        r = self.analizar(DOS_INI, DOS_FIN)
        e = self.ev(r, 0)
        self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
        self.assertEqual(e.resultado.puntos, [])
        self.assertIn("carpeta de esta LLAVE", e.inconsistencia)

    def test_I2_llave_ausente_de_los_manifiestos_que_cubren(self):
        self.adquirir(SEM_INI, SEM_FIN, {1: semana()})
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=2)
        self.assertEqual(self.ev(r, 0).estado_datos, DATOS_COBERTURA_LOCAL_INSUFICIENTE)
        self.assertEqual(self.ev(r, 1).estado_datos, DATOS_OK)
        self.assertFalse(r.sin_cobertura)

    def test_AK_rutas_inseguras_del_manifiesto_se_rechazan(self):
        for ruta in ("../../secreto.gpx", "C:\\Windows\\x.gpx", "/etc/x.gpx", "entrada/x/dato.txt"):
            with self.subTest(ruta=ruta):
                self.poner_manifiesto(SEM_INI, SEM_FIN, [entrada(llave(0), "DESCARGADO", ruta)])
                e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
                self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
                self.assertIn("insegura", e.inconsistencia)

    def test_semantica_sin_datos_en_adquisicion_mayor_vs_exacta(self):
        self.poner_manifiesto(SEM_INI, SEM_FIN, [entrada(llave(0), "SIN_DATOS_PERIODO", actual="ONLINE")])
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.estado_datos, DATOS_SIN_POSICIONES_SUBPERIODO)
        self.assertEqual(e.estado_adquisicion, "SIN_DATOS_PERIODO")  # la observación original se conserva aparte
        self.assertIn("no se consultó BAANOOL para este subperiodo", e.motivos[0])
        self.assertIsNone(e.prioridad)
        # Con una adquisición exacta, SIN_DATOS_PERIODO sigue siendo lo que BAANOOL respondió para ese periodo.
        self.poner_manifiesto(DOS_INI, DOS_FIN, [entrada(llave(0), "SIN_DATOS_PERIODO")])
        self.assertEqual(self.ev(self.analizar(DOS_INI, DOS_FIN), 0).estado_datos, DATOS_SIN_DATOS_PERIODO)

    def test_otros_estados_de_adquisicion_se_conservan(self):
        self.poner_manifiesto(SEM_INI, SEM_FIN, [entrada(llave(0), "SOLO_LOCAL"), entrada(llave(1), "ERROR_EXPORTACION")])
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=2)
        self.assertEqual(self.ev(r, 0).estado_datos, DATOS_SIN_DISPOSITIVO)
        self.assertEqual(self.ev(r, 1).estado_datos, DATOS_ERROR_ADQUISICION)


class PruebasManifiestos(Base):
    def test_usa_las_fechas_del_json_no_el_nombre(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()}, nombre="renombrado.json")
        manifiestos, avisos = sp.descubrir_manifiestos(self.raiz, self.manifiestos)
        self.assertEqual([(m.inicio, m.fin) for m in manifiestos], [(SEM_INI, SEM_FIN)])
        self.assertTrue(any("no coincide con su periodo" in a for a in avisos))
        self.assertFalse(self.analizar(DOS_INI, DOS_FIN).sin_cobertura)

    def test_manifiestos_invalidos_se_omiten_con_aviso(self):
        self.manifiestos.mkdir(parents=True)
        (self.manifiestos / "roto.json").write_text("{no es json", encoding="utf-8")
        self.poner_manifiesto(SEM_INI, SEM_FIN, [], nombre="version.json", version_formato=99)
        self.poner_manifiesto(SEM_FIN, SEM_INI, [], nombre="invertido.json")
        self.poner_manifiesto(SEM_INI, SEM_FIN, [], nombre="sinperiodo.json", periodo_inicio_local="ayer")
        self.poner_manifiesto(SEM_INI, SEM_FIN, [entrada(llave(0), "SOLO_LOCAL")] * 2, nombre="dup.json")
        self.poner_manifiesto(SEM_INI, SEM_FIN, [], nombre="zona.json", zona_horaria_local="Asia/Shanghai")
        manifiestos, avisos = sp.descubrir_manifiestos(self.raiz, self.manifiestos)
        self.assertEqual(manifiestos, [])
        self.assertEqual(len(avisos), 6)

    def test_sin_carpeta_de_manifiestos(self):
        self.assertEqual(sp.descubrir_manifiestos(self.raiz, self.manifiestos), ([], []))
        r = self.analizar(DOS_INI, DOS_FIN)
        self.assertTrue(r.sin_cobertura)
        self.assertEqual(r.intervalos_disponibles(), [])

    def test_periodo_invalido(self):
        with self.assertRaises(ValueError):
            self.analizar(DOS_FIN, DOS_INI)


# ---------- filtrado temporal ----------

def P(texto, lat=19.0):
    return Punto(datetime.fromisoformat(texto).replace(tzinfo=ZONA_HORARIA), lat, -100.0, 0.0)


class PruebasFiltradoTemporal(Base):
    def test_J_K_L_excluye_antes_y_despues_e_incluye_los_limites(self):
        pts = [P("2026-10-14 23:59:59"), P("2026-10-15 00:00:00"), P("2026-10-15 12:00:00"),
               P("2026-10-16 23:59:59"), P("2026-10-17 00:00:00")]
        dentro = filtrar_ventana(pts, DOS_INI, DOS_FIN)
        self.assertEqual([p.fecha.strftime("%d %H:%M:%S") for p in dentro],
                         ["15 00:00:00", "15 12:00:00", "16 23:59:59"])

    def test_P_no_altera_los_timestamps_originales(self):
        pts = [P("2026-10-15 08:00:00"), P("2026-10-15 08:05:00")]
        self.assertEqual(filtrar_ventana(pts, DOS_INI, DOS_FIN), pts)
        self.assertTrue(all(p.fecha.tzinfo is ZONA_HORARIA for p in filtrar_ventana(pts, DOS_INI, DOS_FIN)))

    def test_M_dos_dias_dentro_de_gpx_semanal(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        esperados = [p for p in semana() if DOS_INI <= p[0] <= DOS_FIN]
        self.assertEqual(e.resultado.resumen.cantidad_puntos, len(esperados))
        self.assertEqual(e.resultado.resumen.fecha_inicial.date(), date(2026, 10, 15))
        self.assertEqual(e.resultado.resumen.fecha_final.date(), date(2026, 10, 16))
        self.assertEqual(e.resultado.gpx[0].puntos_fuera_ventana, len(semana()) - len(esperados))

    def test_N_cambio_de_medianoche_no_usa_el_punto_externo(self):
        pts = [(datetime(2026, 10, 14, 23, 58), 19.00, -100.0), (datetime(2026, 10, 15, 0, 3), 19.045, -100.0),
               (datetime(2026, 10, 15, 0, 10), 19.09, -100.0), (datetime(2026, 10, 15, 0, 15), 19.135, -100.0)]
        self.adquirir(SEM_INI, SEM_FIN, {0: pts})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.resultado.resumen.cantidad_puntos, 3)
        self.assertAlmostEqual(e.distancia_observada_km, distancia(pts[1:]), places=6)  # sin el tramo A->B (T)
        self.assertLess(e.distancia_observada_km, distancia(pts))
        self.assertEqual(set(e.distancia_por_dia), {date(2026, 10, 15)})

    def test_O_cambio_de_mes_y_de_anio(self):
        ini, fin = datetime(2026, 12, 29, 0, 0, 0), datetime(2027, 1, 4, 23, 59, 59)
        dias = [date(2026, 12, 29), date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)]
        pts = [p for i, d in enumerate(dias) for p in viaje(d, 2 + i)]
        self.adquirir(ini, fin, {0: pts})
        e = self.ev(self.analizar(datetime(2026, 12, 31, 0, 0, 0), datetime(2027, 1, 1, 23, 59, 59)), 0)
        self.assertEqual(set(e.distancia_por_dia), {date(2026, 12, 31), date(2027, 1, 1)})
        esperado = distancia(viaje(dias[2], 4)) + distancia(viaje(dias[3], 5))
        self.assertAlmostEqual(e.distancia_observada_km, esperado, places=6)
        # Mes: 30 sep -> 1 oct
        ini2, fin2 = datetime(2026, 9, 28, 0, 0, 0), datetime(2026, 10, 3, 23, 59, 59)
        self.adquirir(ini2, fin2, {0: viaje(date(2026, 9, 30), 3) + viaje(date(2026, 10, 1), 3)})
        e2 = self.ev(self.analizar(datetime(2026, 9, 30, 0, 0, 0), datetime(2026, 10, 1, 23, 59, 59)), 0)
        self.assertEqual(set(e2.distancia_por_dia), {date(2026, 9, 30), date(2026, 10, 1)})

    def test_Q_gpx_con_puntos_fuera_pero_ninguno_dentro(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana(dias=[12, 13, 18])})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.estado_datos, DATOS_SIN_POSICIONES_SUBPERIODO)
        self.assertIsNone(e.prioridad)
        self.assertIsNone(e.distancia_observada_km)
        self.assertIsNone(e.nivel_movimiento)
        self.assertIn("No se encontraron posiciones GPS dentro del intervalo seleccionado en los archivos locales "
                      "disponibles.", e.motivos[0])
        self.assertEqual(e.resultado.puntos, [])
        self.assertFalse(e.tiene_datos)
        for texto in ("averiado", "estacionado", "inactivo", "desactivado"):
            self.assertNotIn(texto, e.motivos[0].lower())

    def test_R_un_solo_punto_valido_dentro_del_intervalo(self):
        pts = [(datetime(2026, 10, 14, 23, 0), 19.0, -100.0), (datetime(2026, 10, 15, 9, 0), 19.5, -100.0),
               (datetime(2026, 10, 17, 9, 0), 19.9, -100.0)]
        self.adquirir(SEM_INI, SEM_FIN, {0: pts})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.estado_datos, DATOS_OK)
        self.assertEqual(e.resultado.resumen.cantidad_puntos, 1)
        self.assertEqual(e.distancia_observada_km, 0.0)

    def test_limites_exactos_via_analisis_completo(self):
        pts = [(datetime(2026, 10, 14, 23, 59, 59), 19.0, -100.0), (datetime(2026, 10, 15, 0, 0, 0), 19.001, -100.0),
               (datetime(2026, 10, 16, 23, 59, 59), 19.002, -100.0), (datetime(2026, 10, 17, 0, 0, 0), 19.003, -100.0)]
        self.adquirir(SEM_INI, SEM_FIN, {0: pts})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.resultado.resumen.cantidad_puntos, 2)

    def test_analizar_gpx_sin_ventana_no_cambia(self):
        ruta = self.poner_gpx(0, SEM_INI, SEM_FIN, semana())
        completo = analizar_archivos(self.raiz / ruta.rsplit("/", 1)[0], [self.raiz / ruta], llave(0), None)
        self.assertEqual(completo.resumen.cantidad_puntos, len(semana()))
        self.assertEqual(completo.gpx[0].puntos_fuera_ventana, 0)


# ---------- integridad del análisis ----------

class PruebasIntegridad(Base):
    def test_S_T_U_distancia_solo_con_puntos_seleccionados_sin_duplicar(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        self.adquirir(DOS_INI, datetime(2026, 10, 17, 23, 59, 59), {0: semana(dias=range(15, 18))})  # solapada
        r = self.analizar(DOS_INI, DOS_FIN)
        e = self.ev(r, 0)
        esperado = distancia(viaje(date(2026, 10, 15), 5)) + distancia(viaje(date(2026, 10, 16), 6))
        self.assertAlmostEqual(e.distancia_observada_km, esperado, places=6)
        semanal = distancia(viaje(date(2026, 10, 12), 2)) + sum(distancia(viaje(date(2026, 10, d), d - 10))
                                                                for d in range(13, 19))
        self.assertNotAlmostEqual(e.distancia_observada_km, semanal * 2 / 7, places=1)  # no es prorrateo
        self.assertEqual(e.resultado.resumen.cantidad_puntos, 11 + 13)  # 15 oct: 11 pts, 16 oct: 13 pts (una sola fuente)
        self.assertEqual(len(e.resultado.gpx), 1)
        self.assertEqual(e.resultado.duplicados_eliminados, 0)

    def test_V_geocercas_solo_sobre_el_subperiodo(self):
        # El día 13 el vehículo está en Hidalgo (fuera de la zona A) 13 puntos seguidos; el 15-16 está en zona.
        pts = semana(dias=[12, 14, 15, 16]) + viaje(date(2026, 10, 13), 3, lat0=26.0)
        self.adquirir(SEM_INI, SEM_FIN, {0: pts})
        dentro = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(dentro.salidas_confirmadas, 0)
        self.assertEqual(dentro.entidades_fuera, ())
        con_salida = self.ev(self.analizar(datetime(2026, 10, 12, 0, 0, 0), datetime(2026, 10, 14, 23, 59, 59)), 0)
        self.assertGreaterEqual(con_salida.salidas_confirmadas, 1)
        self.assertIn("Hidalgo", con_salida.entidades_fuera)

    def test_W_calidad_gps_solo_sobre_los_puntos_seleccionados(self):
        salto = [(datetime(2026, 10, 14, 8, 22), 22.0, -100.0)]  # un punto absurdo el día 14
        pts = sorted(semana() + salto)
        self.adquirir(SEM_INI, SEM_FIN, {0: pts})
        con_salto = self.ev(self.analizar(datetime(2026, 10, 14, 0, 0, 0), datetime(2026, 10, 14, 23, 59, 59)), 0)
        self.assertGreaterEqual(len(con_salto.resultado.calidad_gps.saltos_probables)
                                + len(con_salto.resultado.calidad_gps.anomalias_complejas)
                                + len(con_salto.resultado.calidad_gps.tramos_velocidad_sospechosa), 1)
        sin_salto = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        q = sin_salto.resultado.calidad_gps
        self.assertEqual((len(q.saltos_probables), len(q.anomalias_complejas), len(q.tramos_velocidad_sospechosa)),
                         (0, 0, 0))
        self.assertFalse(q.hay_alertas)

    def test_X_los_umbrales_diarios_no_cambian(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        for d, km in e.distancia_por_dia.items():
            self.assertEqual(e.nivel_por_dia[d], nivel_movimiento(km))
        self.assertEqual([nivel_movimiento(x) for x in (99.9, 100, 300, 300.1, 400, 400.1)],
                         ["BAJO", "NORMAL", "NORMAL", "ALTO", "ALTO", "MUY ALTO"])

    def test_Y_sin_posiciones_no_genera_normal_ficticio(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana(dias=[12])})
        r = self.analizar(DOS_INI, DOS_FIN)
        e = self.ev(r, 0)
        self.assertIsNone(e.prioridad)
        s = resumir_flota(r.evaluaciones)
        self.assertEqual((s.por_prioridad["NORMAL"], s.analizados, s.sin_posiciones_subperiodo), (0, 0, 1))
        self.assertEqual(filtrar_evaluaciones(r.evaluaciones, solo_atencion=True), [])
        self.assertEqual(filtrar_evaluaciones(r.evaluaciones, problemas_datos=True), r.evaluaciones)

    def test_Z_un_dia_sin_puntos_no_es_inmovilidad_confirmada(self):
        pts = viaje(date(2026, 10, 15), 3) + viaje(date(2026, 10, 17), 3)
        self.adquirir(SEM_INI, SEM_FIN, {0: pts})
        ini, fin = datetime(2026, 10, 15, 0, 0, 0), datetime(2026, 10, 17, 23, 59, 59)
        e = self.ev(self.analizar(ini, fin), 0)
        filas = {f["fecha"]: f for f in sp.filas_movimiento_diario(e, ini, fin)}
        self.assertEqual(filas[date(2026, 10, 16)]["puntos"], 0)
        self.assertIsNone(filas[date(2026, 10, 16)]["distancia_km"])
        self.assertIsNone(filas[date(2026, 10, 16)]["nivel"])
        self.assertIn("Sin posiciones", filas[date(2026, 10, 16)]["observacion"])
        self.assertIsNotNone(filas[date(2026, 10, 15)]["distancia_km"])
        self.assertEqual(filas[date(2026, 10, 17)]["observacion"], "")

    def test_Z2_dias_en_los_extremos_sin_puntos_tambien_se_listan(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: viaje(date(2026, 10, 15), 3)})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        filas = sp.filas_movimiento_diario(e, DOS_INI, DOS_FIN)
        self.assertEqual([f["fecha"] for f in filas], [date(2026, 10, 15), date(2026, 10, 16)])
        self.assertEqual(filas[1]["puntos"], 0)
        self.assertEqual(sp.filas_movimiento_diario(self.ev(self.analizar(DOS_INI, DOS_FIN), 0), DOS_INI, DOS_FIN)[1]["nivel"], None)

    def test_AL_no_se_asignan_puntos_de_otra_llave(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana(lat0=19.0), 1: semana(lat0=21.0)})
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=2)
        for n, lat0 in ((0, 19.0), (1, 21.0)):
            e = self.ev(r, n)
            self.assertTrue(e.resultado.puntos)
            self.assertTrue(all(lat0 <= p.latitud <= lat0 + 0.5 for p in e.resultado.puntos))
            self.assertEqual(r.fuentes[llave(n)].archivo.parent.name, limpiar_nombre_carpeta(llave(n)))

    def test_exacta_se_analiza_completa_como_antes(self):
        # Un punto de un GPX exacto fuera del rango declarado sigue participando (comportamiento histórico).
        pts = viaje(date(2026, 10, 15), 3) + [(datetime(2026, 10, 17, 3, 0), 19.0, -100.0)]
        self.adquirir(DOS_INI, DOS_FIN, {0: pts})
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.resultado.resumen.cantidad_puntos, len(pts))

    def test_sin_posiciones_en_gpx_con_error_sigue_siendo_error(self):
        ruta = self.poner_gpx(0, SEM_INI, SEM_FIN, semana())
        (self.raiz / ruta).write_bytes(b"no es xml")
        self.poner_manifiesto(SEM_INI, SEM_FIN, [entrada(llave(0), "DESCARGADO", ruta)])
        e = self.ev(self.analizar(DOS_INI, DOS_FIN), 0)
        self.assertEqual(e.estado_datos, "ERROR")
        self.assertIsNone(e.prioridad)


# ---------- integración ----------

class PruebasIntegracion(Base):
    def montar_flota(self):
        """Cinco LLAVEs: con datos, sin posiciones en el subperiodo, solo local, con error, y ausente."""
        self.adquirir(SEM_INI, SEM_FIN, {0: semana(), 1: semana(dias=[12])})
        m = json.loads((self.manifiestos / nombre_manifiesto(SEM_INI, SEM_FIN)).read_text(encoding="utf-8"))
        m["vehiculos"] += [entrada(llave(2), "SOLO_LOCAL"), entrada(llave(3), "ERROR_EXPORTACION")]
        (self.manifiestos / nombre_manifiesto(SEM_INI, SEM_FIN)).write_text(json.dumps(m), encoding="utf-8")
        return 5  # la LLAVE 4 no está en el manifiesto

    def test_AA_se_representan_todas_las_llaves_locales(self):
        n = self.montar_flota()
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=n)
        estados = {e.identificador: e.estado_datos for e in r.evaluaciones}
        self.assertEqual(len(r.evaluaciones), 5)
        self.assertEqual(estados, {llave(0): DATOS_OK, llave(1): DATOS_SIN_POSICIONES_SUBPERIODO,
                                   llave(2): DATOS_SIN_DISPOSITIVO, llave(3): DATOS_ERROR_ADQUISICION,
                                   llave(4): DATOS_COBERTURA_LOCAL_INSUFICIENTE})
        self.assertEqual([e.prioridad is None for e in r.evaluaciones].count(True), 4)
        self.assertEqual(set(r.fuentes), {llave(i) for i in range(5)})

    def test_AB_AC_no_hay_peticiones_ni_se_necesita_token(self):
        self.montar_flota()
        prohibido = mock.Mock(side_effect=AssertionError("el análisis local no debe consultar BAANOOL"))
        with mock.patch.object(requests.Session, "request", prohibido), \
                mock.patch.object(requests, "get", prohibido), mock.patch.object(requests, "post", prohibido), \
                mock.patch.object(ip, "crear_sesion", prohibido), mock.patch.object(ip, "obtener_token", prohibido), \
                mock.patch.object(ip, "actualizar_desde_baanool", prohibido), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAANOOL_TOKEN", None)
            r = self.analizar(DOS_INI, DOS_FIN, n_llaves=5)
        prohibido.assert_not_called()
        self.assertEqual(r.solicitudes_baanool, 0)
        self.assertFalse(ip.token_configurado())

    def test_AB2_cobertura_insuficiente_tampoco_descarga(self):
        prohibido = mock.Mock(side_effect=AssertionError("no debe consultar BAANOOL"))
        with mock.patch.object(requests.Session, "request", prohibido), \
                mock.patch.object(ip, "actualizar_desde_baanool", prohibido):
            r = self.analizar(DOS_INI, DOS_FIN)
        self.assertTrue(r.sin_cobertura)
        prohibido.assert_not_called()
        self.assertFalse(any((self.raiz / "entrada").glob("*/*.gpx")))  # no se descargó nada

    def test_AD_AE_no_se_modifican_gpx_ni_manifiestos_ni_se_crean_archivos(self):
        self.montar_flota()
        antes = self.huella()
        self.analizar(DOS_INI, DOS_FIN, n_llaves=5)
        self.analizar(datetime(2026, 10, 13, 6, 0, 0), datetime(2026, 10, 14, 6, 0, 0), n_llaves=5)
        despues = self.huella()
        nuevos = set(despues) - set(antes)
        self.assertEqual({k for k in nuevos if not k.endswith(".xlsx")}, set())  # solo el Excel de la fixture
        self.assertEqual({k: v for k, v in despues.items() if k in antes}, antes)

    def test_AF_la_sesion_identifica_el_periodo_de_adquisicion_usado(self):
        self.montar_flota()
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=5)
        estado = {}
        sp.aplicar_resultado_subperiodo(estado, r, resumir_flota(r.evaluaciones), generar_csv(r.evaluaciones),
                                        datetime(2026, 10, 21, 12, 0, 0))
        for clave in ip.CLAVES_ANALISIS:
            self.assertIn(clave, estado)
        fila = estado["origen_datos"]["resumen"][0]
        self.assertEqual((fila["adquisicion_inicio"], fila["adquisicion_fin"], fila["tipo"], fila["vehiculos"]),
                         (SEM_INI, SEM_FIN, sp.TIPO_MAYOR, 2))
        self.assertTrue(estado["origen_datos"]["usa_mayor"])
        self.assertEqual(estado["origen_datos"]["solicitudes_baanool"], 0)
        self.assertEqual(estado["periodo_analizado"]["inicio"], "2026-10-15 00:00:00")
        self.assertEqual(estado["periodo_analizado"]["modo"], "PERIODO")
        self.assertEqual(estado["fuentes_datos"][llave(0)].manifiesto, f"salida/estado_descargas/"
                                                                       f"{nombre_manifiesto(SEM_INI, SEM_FIN)}")
        texto = repr(estado["origen_datos"]) + repr(estado["fuentes_datos"])
        for prohibido in ("token", "https://", "DNO", "dno"):
            self.assertNotIn(prohibido, texto)

    def test_AF2_distintas_adquisiciones_para_distintas_llaves(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        self.adquirir(DOS_INI, DOS_FIN, {1: viaje(date(2026, 10, 15), 3)})
        self.adquirir(SEM_INI, SEM_FIN, {1: semana(lat0=21.0)}, nombre="otra_ejecucion_sem.json",
                      ejecucion="2026-10-01T00:00:00-06:00")
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=2)
        self.assertEqual({o["tipo"]: o["vehiculos"] for o in r.resumen_origen()},
                         {sp.TIPO_EXACTA: 1, sp.TIPO_MAYOR: 1})

    def test_AG_los_filtros_no_vuelven_a_analizar(self):
        self.montar_flota()
        r = self.analizar(DOS_INI, DOS_FIN, n_llaves=5)
        prohibido = mock.Mock(side_effect=AssertionError("los filtros no deben analizar"))
        with mock.patch.object(sp, "analizar_archivos", prohibido), \
                mock.patch.object(sp, "analizar_periodo_local", prohibido), \
                mock.patch("src.analizador.leer_gpx", prohibido):
            filtrar_evaluaciones(r.evaluaciones, solo_atencion=True)
            filtrar_evaluaciones(r.evaluaciones, problemas_datos=True, texto="PLACA01")
            filtrar_evaluaciones(r.evaluaciones, estados_datos=[DATOS_SIN_POSICIONES_SUBPERIODO])
            sp.filas_movimiento_diario(r.evaluaciones[0], DOS_INI, DOS_FIN)
        prohibido.assert_not_called()

    def test_AG2_cada_gpx_se_lee_una_sola_vez(self):
        self.montar_flota()
        from src import analizador
        real = analizador.leer_gpx
        with mock.patch("src.analizador.leer_gpx", side_effect=real) as espia:
            self.analizar(DOS_INI, DOS_FIN, n_llaves=5)
        self.assertEqual(espia.call_count, 2)  # LLAVEs 0 y 1; el resto no abre ningún archivo

    def test_AH_los_mapas_siguen_siendo_perezosos(self):
        self.montar_flota()
        with mock.patch("src.mapa.generar_mapa") as mapa:
            r = self.analizar(DOS_INI, DOS_FIN, n_llaves=5)
            estado = {}
            sp.aplicar_resultado_subperiodo(estado, r, resumir_flota(r.evaluaciones), "", datetime.now())
        mapa.assert_not_called()
        self.assertEqual((estado["mapas"], estado["mapa_abierto"]), ({}, None))
        por_llave = {e.identificador: e for e in r.evaluaciones}
        self.assertTrue(ip.puede_generar_mapa(por_llave[llave(0)]))
        self.assertFalse(ip.puede_generar_mapa(por_llave[llave(1)]))  # sin posiciones: no hay mapa

    def test_AI_el_modo_local_y_el_periodo_exacto_anteriores_siguen_funcionando(self):
        self.adquirir(DOS_INI, DOS_FIN, {0: viaje(date(2026, 10, 15), 3)})
        local = ip.analizar_flota_local(self.entrada, self.excel(1), ZONAS)
        self.assertEqual([e.estado_datos for e in local], [DATOS_OK])
        antiguo = ip.analizar_periodo(DOS_INI, DOS_FIN, self.excel(1), self.raiz, ZONAS, self.manifiestos)
        nuevo = self.analizar(DOS_INI, DOS_FIN)
        self.assertAlmostEqual(antiguo.evaluaciones[0].distancia_observada_km,
                               nuevo.evaluaciones[0].distancia_observada_km)
        # El resultado de aplicar_resultado_analisis (modo anterior) limpia la procedencia del subperiodo.
        estado = {"origen_datos": {"x": 1}, "fuentes_datos": {"a": 1}}
        ip.aplicar_resultado_analisis(estado, antiguo, resumir_flota(antiguo.evaluaciones), "", datetime.now())
        self.assertEqual((estado["origen_datos"], estado["fuentes_datos"]), (None, {}))

    def test_AJ_el_csv_refleja_el_subperiodo_no_la_semana(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        r = self.analizar(DOS_INI, DOS_FIN)
        filas = list(csv.DictReader(generar_csv(r.evaluaciones).splitlines()))
        esperado = distancia(viaje(date(2026, 10, 15), 5)) + distancia(viaje(date(2026, 10, 16), 6))
        self.assertEqual(float(filas[0]["distancia_estimada_observada"]), round(esperado, 2))
        self.assertEqual(filas[0]["estado_adquisicion"], "DESCARGADO")

    def test_subperiodos_distintos_de_la_misma_semana_sin_volver_a_descargar(self):
        self.adquirir(SEM_INI, SEM_FIN, {0: semana()})
        d1 = self.ev(self.analizar(datetime(2026, 10, 13, 0, 0, 0), datetime(2026, 10, 14, 23, 59, 59)), 0)
        d2 = self.ev(self.analizar(datetime(2026, 10, 13, 0, 0, 0), datetime(2026, 10, 15, 23, 59, 59)), 0)
        self.assertEqual(set(d1.distancia_por_dia), {date(2026, 10, 13), date(2026, 10, 14)})
        self.assertEqual(set(d2.distancia_por_dia), {date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)})
        self.assertGreater(d2.distancia_observada_km, d1.distancia_observada_km)


if __name__ == "__main__":
    unittest.main()
