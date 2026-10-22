"""Pruebas de la descarga por lote (etapa 6B-2). HTTP simulado: NO hay llamadas reales.

    py -m unittest tests.test_descarga_lote -v
"""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import requests

from crear_carpetas_llaves import limpiar_nombre_carpeta
from src import baanool
from src.baanool import URL_EXPORTACION, USER_AGENT_BAANOOL, construir_referer
from src.descarga_lote import (CARPETA_AMBIGUA, DESCARGADO, ERROR_DESCARGA, ERROR_EXPORTACION, GPX_INVALIDO,
                               LOTE_COMPLETADO, LOTE_DETENIDO_RATE_LIMIT, LOTE_DETENIDO_TOKEN, LOTE_DRY_RUN,
                               POR_DESCARGAR, SIN_COORDENADAS, SIN_DATOS_PERIODO, YA_EXISTE,
                               coordenadas_validas, ejecutar_lote, nombre_archivo_lote)
from src.mapeo_baanool import mapear_llaves_con_dispositivos

TOKEN = "SENTINELA-TOKEN-NO-REAL-0003"
INICIO, FIN = datetime(2026, 10, 10, 0, 0, 0), datetime(2026, 10, 10, 23, 59, 59)
NOMBRE = "baanool_2026-10-10_000000_2026-10-10_235959.gpx"

GPX_3 = b"""<?xml version="1.0" encoding="UTF-8"?><gpx><trk><trkseg>
<trkpt lat="19.30" lon="-99.24"><ele>0.0</ele><time>2026-10-10 00:02:53</time></trkpt>
<trkpt lat="19.31" lon="-99.25"><ele>0.0</ele><time>2026-10-10 00:07:53</time></trkpt>
<trkpt lat="19.32" lon="-99.26"><ele>0.0</ele><time>2026-10-10 00:12:53</time></trkpt>
</trkseg></trk></gpx>"""
GPX_1 = b"""<?xml version="1.0"?><gpx><trk><trkseg>
<trkpt lat="19.30" lon="-99.24"><ele>0.0</ele><time>2026-10-10 08:00:00</time></trkpt>
</trkseg></trk></gpx>"""
GPX_VACIO = b'<?xml version="1.0"?><gpx><trk><trkseg></trkseg></trk></gpx>'


class Resp:
    def __init__(self, status_code=200, json_data=None, contenido=b""):
        self.status_code, self._json, self.content, self.headers = status_code, json_data, contenido, {}

    def json(self):
        return self._json


def exportacion_ok(dno):
    return Resp(200, {"status": 0, "resmsg": "SUCCESS", "data":
                f"https://www.baanooliot.com/bnFile/downloadFileByName?bucktName=icon&fileName={dno}.gpx"})


class SesionLote:
    """Simula BAANOOL: exportación por DNO y descarga por URL. Registra todo."""

    def __init__(self, exportaciones=None, archivos=None):
        self.exportaciones = exportaciones or {}  # dno -> Resp | Exception
        self.archivos = archivos or {}  # dno -> bytes | Resp | Exception
        self.llamadas_exportacion = []
        self.llamadas_descarga = []

    def get(self, url, params=None, headers=None, timeout=None):
        if url == URL_EXPORTACION:
            dno = params["dno"]
            self.llamadas_exportacion.append({"dno": dno, "params": params, "headers": headers})
            actual = self.exportaciones.get(dno) or exportacion_ok(dno)
        else:
            dno = url.rsplit("fileName=", 1)[-1].removesuffix(".gpx")
            self.llamadas_descarga.append({"dno": dno, "url": url, "headers": headers})
            actual = self.archivos.get(dno, GPX_3)
            if isinstance(actual, bytes):
                actual = Resp(200, contenido=actual)
        if isinstance(actual, BaseException):
            raise actual
        return actual

    @property
    def dnos_exportados(self):
        return [c["dno"] for c in self.llamadas_exportacion]


def disp(i, name, dno, lat=19.4, lng=-99.1, car=None, online=True):
    return baanool._convertir_dispositivo(
        {"name": name, "dno": dno, "carNum": car, "lat": lat, "lng": lng, "online": online}, i)


def llave(n):
    return f"AVEO|2022|AZUL|PLACA{n:02d}"


def dno(n):
    return f"8640350541{n:05d}"


def mapeo_con(n_exactas, locales_extra=(), dispositivos_extra=(), override=None):
    """n_exactas vehículos exactos (LLAVE == name) + extras. override: n -> dict de campos (lat, lng...)."""
    override = override or {}
    llaves = [llave(n) for n in range(n_exactas)] + list(locales_extra)
    dispositivos = [disp(n, llave(n), dno(n), **override.get(n, {})) for n in range(n_exactas)]
    dispositivos += [disp(n_exactas + k, *args) for k, args in enumerate(dispositivos_extra)]
    return mapear_llaves_con_dispositivos(llaves, dispositivos)


class BaseLote(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entrada = Path(self._tmp.name) / "entrada"
        self.entrada.mkdir()
        self.pausas = []

    def tearDown(self):
        self._tmp.cleanup()

    def correr(self, mapeo, sesion=None, **kwargs):
        sesion = sesion or SesionLote()
        kwargs.setdefault("token", TOKEN)
        lote = ejecutar_lote(mapeo, INICIO, FIN, self.entrada, sesion=sesion, pausar=self.pausas.append, **kwargs)
        return lote, sesion

    def archivo(self, n):
        return self.entrada / limpiar_nombre_carpeta(llave(n)) / NOMBRE


class PruebasElegibilidad(BaseLote):
    def test_A_solamente_exactos_son_elegibles(self):
        mapeo = mapeo_con(3, locales_extra=["SOLO|LOCAL|X|1"], dispositivos_extra=[("SOLO|BAANOOL|X|2", dno(90))])
        lote, sesion = self.correr(mapeo)
        self.assertEqual(sorted(sesion.dnos_exportados), sorted(dno(n) for n in range(3)))
        self.assertEqual(lote.exactas_totales, 3)
        self.assertEqual(lote.estado, LOTE_COMPLETADO)

    def test_B_solo_local_no_se_descarga(self):
        lote, sesion = self.correr(mapeo_con(2, locales_extra=["SOLO|LOCAL|A|1", "SOLO|LOCAL|B|2"]))
        self.assertEqual(len(sesion.llamadas_exportacion), 2)
        self.assertNotIn("SOLO|LOCAL|A|1", [v.llave for v in lote.vehiculos])

    def test_C_solo_baanool_no_se_descarga(self):
        mapeo = mapeo_con(1, dispositivos_extra=[("NO|ESTA|EN|EXCEL", dno(77))])
        lote, sesion = self.correr(mapeo)
        self.assertEqual(sesion.dnos_exportados, [dno(0)])
        self.assertNotIn(dno(77), sesion.dnos_exportados)

    def test_D_posible_coincidencia_no_se_descarga(self):
        mapeo = mapeo_con(1, locales_extra=["AVEO/2026/BLANCO/NJG463C"],
                          dispositivos_extra=[("AVEO|2026|BLANCO|NJG463C", dno(80), 19.4, -99.1, "NJG463C")])
        self.assertEqual(len(mapeo.con_estado("POSIBLE_COINCIDENCIA")), 1)
        lote, sesion = self.correr(mapeo)
        self.assertEqual(sesion.dnos_exportados, [dno(0)])
        self.assertNotIn(dno(80), sesion.dnos_exportados)

    def test_E_conflicto_no_se_descarga(self):
        mapeo = mapeo_con(1, locales_extra=["DUP|NAME|X|1"],
                          dispositivos_extra=[("DUP|NAME|X|1", dno(81)), ("DUP|NAME|X|1", dno(82))])
        self.assertEqual(len(mapeo.con_estado("CONFLICTO")), 1)
        lote, sesion = self.correr(mapeo)
        self.assertEqual(sesion.dnos_exportados, [dno(0)])

    def test_V_los_solo_local_quedan_fuera_del_lote(self):
        extras = [f"SOLO|LOCAL|{k}|X" for k in range(7)]
        mapeo = mapeo_con(5, locales_extra=extras)
        self.assertEqual(mapeo.resumen()["solo_local"], 7)
        lote, sesion = self.correr(mapeo)
        self.assertEqual(len(sesion.llamadas_exportacion), 5)  # no se asume ningún número fijo
        self.assertEqual(lote.exactas_totales, 5)
        self.assertEqual(lote.resumen_mapeo["solo_local"], 7)
        for extra in extras:
            self.assertFalse((self.entrada / limpiar_nombre_carpeta(extra)).exists())  # ni carpeta se crea


class PruebasPeticionYDestino(BaseLote):
    def test_F_lat_lng_correctos_se_pasan_al_referer_de_cada_dispositivo(self):
        mapeo = mapeo_con(2, override={0: {"lat": 19.405906, "lng": -99.113198}, 1: {"lat": 19.5, "lng": -99.2}})
        _, sesion = self.correr(mapeo)
        por_dno = {c["dno"]: c for c in sesion.llamadas_exportacion}
        self.assertEqual(por_dno[dno(0)]["headers"]["Referer"], construir_referer(dno(0), 19.405906, -99.113198))
        self.assertEqual(por_dno[dno(1)]["headers"]["Referer"], construir_referer(dno(1), 19.5, -99.2))
        for c in sesion.llamadas_exportacion:  # la configuración validada en 6A
            self.assertEqual(c["headers"]["token"], TOKEN)
            self.assertEqual(c["headers"]["User-Agent"], USER_AGENT_BAANOOL)
            self.assertNotIn("Cookie", c["headers"])
            self.assertEqual(c["params"]["exportType"], "2")

    def test_G_sin_lat_lng_es_sin_coordenadas(self):
        mapeo = mapeo_con(4, override={0: {"lat": None, "lng": None}, 1: {"lat": 19.4, "lng": None},
                                       2: {"lat": 0.0, "lng": 0.0}, 3: {"lat": 120.0, "lng": -99.0}})
        lote, sesion = self.correr(mapeo)
        self.assertEqual([v.estado for v in lote.vehiculos], [SIN_COORDENADAS] * 4)
        self.assertEqual(sesion.llamadas_exportacion, [])  # nada se intentó, nada se inventó
        self.assertFalse(coordenadas_validas(float("nan"), -99.0))
        self.assertTrue(coordenadas_validas(19.4, -99.1))

    def test_H_usa_limpiar_nombre_carpeta(self):
        lote, _ = self.correr(mapeo_con(1))
        esperado = self.entrada / limpiar_nombre_carpeta(llave(0)) / NOMBRE
        self.assertEqual(lote.vehiculos[0].ruta_archivo, esperado)
        self.assertTrue(esperado.is_file())
        self.assertEqual(esperado.parent.name, "AVEO - 2022 - AZUL - PLACA00")  # '|' -> ' - '

    def test_T_nombre_con_fechas_locales_cdmx_y_peticion_convertida(self):
        self.assertEqual(nombre_archivo_lote(INICIO, FIN), NOMBRE)
        self.assertNotIn("864", NOMBRE)  # sin DNO
        lote, sesion = self.correr(mapeo_con(1))
        self.assertEqual(lote.vehiculos[0].ruta_archivo.name, NOMBRE)
        # A BAANOOL se le envía la hora convertida (+14 h); el nombre conserva la local.
        self.assertEqual(sesion.llamadas_exportacion[0]["params"]["startTime"], "2026-10-10 14:00:00")

    def test_J_gpx_valido_es_descargado(self):
        lote, _ = self.correr(mapeo_con(1))
        v = lote.vehiculos[0]
        self.assertEqual((v.estado, v.cantidad_puntos), (DESCARGADO, 3))
        self.assertEqual(v.ruta_archivo.read_bytes(), GPX_3)

    def test_K_gpx_de_un_punto_es_descargado(self):
        sesion = SesionLote(archivos={dno(0): GPX_1})
        lote, _ = self.correr(mapeo_con(1), sesion)
        self.assertEqual((lote.vehiculos[0].estado, lote.vehiculos[0].cantidad_puntos), (DESCARGADO, 1))


class PruebasExistentesYSeguridadDeArchivos(BaseLote):
    def test_I_ya_existe_no_llama_a_baanool_ni_sobrescribe(self):
        carpeta = self.entrada / limpiar_nombre_carpeta(llave(0))
        carpeta.mkdir()
        (carpeta / NOMBRE).write_bytes(b"CONTENIDO-PREVIO")
        lote, sesion = self.correr(mapeo_con(2))
        estados = {v.llave: v.estado for v in lote.vehiculos}
        self.assertEqual(estados[llave(0)], YA_EXISTE)
        self.assertEqual(sesion.dnos_exportados, [dno(1)])  # solo el otro
        self.assertEqual((carpeta / NOMBRE).read_bytes(), b"CONTENIDO-PREVIO")
        self.assertEqual(sorted(p.name for p in carpeta.iterdir()), [NOMBRE])  # sin _1, _2...

    def test_P_los_archivos_previos_no_se_borran(self):
        carpeta = self.entrada / limpiar_nombre_carpeta(llave(0))
        carpeta.mkdir()
        previos = {"F86AYT.gpx": b"GPX-MANUAL", "baanool_2026-10-09_000000_2026-10-09_235959.gpx": b"AYER",
                   "notas.txt": b"x"}
        for nombre, contenido in previos.items():
            (carpeta / nombre).write_bytes(contenido)
        otra = self.entrada / "OTRA CARPETA"
        otra.mkdir()
        (otra / "a.gpx").write_bytes(b"AJENO")
        self.correr(mapeo_con(1))
        for nombre, contenido in previos.items():
            self.assertEqual((carpeta / nombre).read_bytes(), contenido)
        self.assertEqual((otra / "a.gpx").read_bytes(), b"AJENO")

    def test_carpeta_ambigua_no_descarga_ninguna(self):
        # Dos LLAVEs exactas distintas que producen la misma carpeta local.
        llaves = ["A|B", "A - B"]
        mapeo = mapear_llaves_con_dispositivos(llaves, [disp(0, "A|B", dno(1)), disp(1, "A - B", dno(2))])
        self.assertEqual(len(mapeo.exactas), 2)
        lote, sesion = self.correr(mapeo)
        self.assertEqual([v.estado for v in lote.vehiculos], [CARPETA_AMBIGUA] * 2)
        self.assertEqual(sesion.llamadas_exportacion, [])


class PruebasErroresYDetenciones(BaseLote):
    def test_L_un_error_individual_permite_continuar(self):
        sesion = SesionLote(
            exportaciones={dno(1): Resp(200, {"status": 1, "resmsg": "fallo puntual", "data": None}),
                           dno(3): requests.exceptions.ConnectTimeout()},
            archivos={dno(2): Resp(404), dno(4): b"<?xml version='1.0'?><gpx><trk><trkpt",
                      dno(5): Resp(200, contenido=b"<html>error</html>")})
        lote, _ = self.correr(mapeo_con(7), sesion)
        estados = {v.llave: v.estado for v in lote.vehiculos}
        self.assertEqual(estados[llave(0)], DESCARGADO)
        self.assertEqual(estados[llave(1)], ERROR_EXPORTACION)
        self.assertEqual(estados[llave(2)], ERROR_DESCARGA)
        self.assertEqual(estados[llave(3)], ERROR_EXPORTACION)
        self.assertEqual(estados[llave(4)], GPX_INVALIDO)
        self.assertEqual(estados[llave(5)], ERROR_DESCARGA)
        self.assertEqual(estados[llave(6)], DESCARGADO)
        self.assertEqual(lote.estado, LOTE_COMPLETADO)
        self.assertEqual(lote.procesados, 7)
        self.assertEqual(len(lote.errores), 5)
        self.assertEqual(lote.pendientes, [])

    def test_M_token_invalido_detiene(self):
        sesion = SesionLote(exportaciones={dno(1): Resp(401)})
        lote, sesion = self.correr(mapeo_con(4), sesion)
        self.assertEqual(lote.estado, LOTE_DETENIDO_TOKEN)
        self.assertEqual(sesion.dnos_exportados, [dno(0), dno(1)])  # no se sigue con el tercero
        self.assertEqual(lote.pendientes, [llave(2), llave(3)])
        self.assertEqual(lote.detenido_en, llave(1))
        self.assertTrue(self.archivo(0).is_file())  # lo descargado antes permanece

    def test_N_token_ausente_detiene_sin_peticiones(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAANOOL_TOKEN", None)
            lote, sesion = self.correr(mapeo_con(3), token=None)
        self.assertEqual(lote.estado, LOTE_DETENIDO_TOKEN)
        self.assertEqual(sesion.llamadas_exportacion, [])
        self.assertEqual(lote.vehiculos, [])
        self.assertEqual(len(lote.pendientes), 3)
        self.assertIn("BAANOOL_TOKEN", lote.motivo_detencion)

    def test_O_rate_limit_detiene_sin_reintentar(self):
        sesion = SesionLote(exportaciones={dno(2): Resp(200, {"status": 5550, "resmsg": "Too many requests"})})
        lote, sesion = self.correr(mapeo_con(5), sesion)
        self.assertEqual(lote.estado, LOTE_DETENIDO_RATE_LIMIT)
        self.assertEqual(sesion.dnos_exportados, [dno(0), dno(1), dno(2)])  # una sola petición al fallido
        self.assertEqual(lote.cuenta(DESCARGADO), 2)
        self.assertEqual(lote.pendientes, [llave(3), llave(4)])
        self.assertEqual(lote.detenido_en, llave(2))
        self.assertTrue(self.archivo(0).is_file() and self.archivo(1).is_file())  # intactos
        self.assertFalse(self.archivo(2).exists())

    def test_token_invalido_en_la_descarga_tambien_detiene(self):
        sesion = SesionLote(archivos={dno(1): Resp(403)})
        lote, _ = self.correr(mapeo_con(3), sesion)
        self.assertEqual(lote.estado, LOTE_DETENIDO_TOKEN)

    def test_permiso_105_es_error_individual_y_continua(self):
        sesion = SesionLote(exportaciones={dno(0): Resp(200, {"status": 105, "resmsg": "暂无"})})
        lote, _ = self.correr(mapeo_con(2), sesion)
        self.assertEqual([v.estado for v in lote.vehiculos], [ERROR_EXPORTACION, DESCARGADO])

    def test_U_el_token_nunca_aparece_en_los_resultados(self):
        sesion = SesionLote(
            exportaciones={dno(0): Resp(200, {"status": 1, "resmsg": f"error {TOKEN}"}),
                           dno(1): requests.exceptions.ConnectionError(f"fallo {TOKEN}"),
                           dno(3): Resp(200, {"status": 5550, "resmsg": TOKEN})},
            archivos={dno(2): Resp(200, contenido=b"<html>" + TOKEN.encode() + b"</html>")})
        lote, _ = self.correr(mapeo_con(5), sesion)
        self.assertEqual(lote.estado, LOTE_DETENIDO_RATE_LIMIT)
        self.assertNotIn(TOKEN, repr(lote))
        self.assertNotIn(TOKEN, str(lote.motivo_detencion))
        for v in lote.vehiculos:
            self.assertNotIn(TOKEN, repr(v))


class PruebasModosYOrden(BaseLote):
    def test_Q_dry_run_no_exporta_ni_crea_nada(self):
        carpeta = self.entrada / limpiar_nombre_carpeta(llave(0))
        carpeta.mkdir()
        (carpeta / NOMBRE).write_bytes(b"YA")
        mapeo = mapeo_con(3, override={2: {"lat": None, "lng": None}})
        lote, sesion = self.correr(mapeo, token=None, dry_run=True)  # el dry-run ni siquiera requiere token
        self.assertEqual(lote.estado, LOTE_DRY_RUN)
        self.assertEqual(sesion.llamadas_exportacion, [])
        self.assertEqual(sesion.llamadas_descarga, [])
        self.assertEqual({v.llave: v.estado for v in lote.vehiculos},
                         {llave(0): YA_EXISTE, llave(1): POR_DESCARGAR, llave(2): SIN_COORDENADAS})
        self.assertFalse((self.entrada / limpiar_nombre_carpeta(llave(1))).exists())  # no crea carpetas
        self.assertEqual(self.pausas, [])  # no espera

    def test_R_limite_procesa_como_maximo_n(self):
        lote, sesion = self.correr(mapeo_con(8), limite=3)
        self.assertEqual(len(sesion.llamadas_exportacion), 3)
        self.assertEqual(lote.seleccionadas, 3)
        self.assertEqual(lote.exactas_totales, 8)
        lote2, sesion2 = self.correr(mapeo_con(2), SesionLote(), limite=5)  # N mayor que las exactas
        self.assertEqual(lote2.seleccionadas, 2)

    def test_S_orden_determinista_por_llave(self):
        llaves = ["ZETA|1|X|1", "ALFA|1|X|1", "MIKE|1|X|1", "BETA|1|X|1"]
        dispositivos = [disp(i, l, dno(i)) for i, l in enumerate(llaves)]
        mapeo = mapear_llaves_con_dispositivos(llaves, dispositivos)
        _, sesion = self.correr(mapeo)
        orden = [next(d.name for d in dispositivos if d.dno == x) for x in sesion.dnos_exportados]
        self.assertEqual(orden, sorted(llaves))
        # --limite toma las primeras por LLAVE (en una carpeta de entrada nueva, sin archivos previos).
        self.entrada = Path(self._tmp.name) / "entrada2"
        self.entrada.mkdir()
        _, sesion2 = self.correr(mapeo, SesionLote(), limite=2)
        self.assertEqual(len(sesion2.llamadas_exportacion), 2)
        self.assertEqual([next(d.name for d in dispositivos if d.dno == x) for x in sesion2.dnos_exportados],
                         ["ALFA|1|X|1", "BETA|1|X|1"])

    def test_pausa_solo_entre_vehiculos_con_peticiones(self):
        carpeta = self.entrada / limpiar_nombre_carpeta(llave(1))
        carpeta.mkdir()
        (carpeta / NOMBRE).write_bytes(b"YA")
        lote, _ = self.correr(mapeo_con(4), pausa_segundos=2)
        # 0 descarga (pausa), 1 ya existe (sin pausa), 2 descarga (pausa), 3 último (sin pausa).
        self.assertEqual(self.pausas, [2, 2])

    def test_progreso_con_denominador_dinamico(self):
        eventos = []
        self.correr(mapeo_con(4), progreso=lambda i, n, r: eventos.append((i, n, r.estado)))
        self.assertEqual([(i, n) for i, n, _ in eventos], [(1, 4), (2, 4), (3, 4), (4, 4)])

    def test_no_se_ejecuta_ningun_analisis_ni_se_modifica_el_excel(self):
        import src.descarga_lote as lote_mod
        self.assertFalse(hasattr(lote_mod, "analizar_flota"))
        self.assertFalse(hasattr(lote_mod, "load_workbook"))


class PruebasSinDatosPeriodo(BaseLote):
    """BAANOOL respondió pero no entregó posiciones: es una observación, no un error."""

    def carpeta(self, n):
        return self.entrada / limpiar_nombre_carpeta(llave(n))

    def test_gpx_sin_posiciones_es_sin_datos_periodo_y_no_error(self):
        sesion = SesionLote(archivos={dno(1): GPX_VACIO})
        lote, _ = self.correr(mapeo_con(3), sesion)
        estados = {v.llave: v.estado for v in lote.vehiculos}
        self.assertEqual(estados, {llave(0): DESCARGADO, llave(1): SIN_DATOS_PERIODO, llave(2): DESCARGADO})
        self.assertEqual(lote.errores, [])  # no cuenta como error
        self.assertEqual(lote.estado, LOTE_COMPLETADO)
        self.assertEqual(lote.cuenta(SIN_DATOS_PERIODO), 1)

    def test_sin_datos_no_deja_ningun_archivo_ni_temporales(self):
        sesion = SesionLote(archivos={dno(0): GPX_VACIO})
        lote, _ = self.correr(mapeo_con(1), sesion)
        self.assertEqual(list(self.carpeta(0).iterdir()), [])  # ni .gpx, ni .descarga, ni .invalido
        self.assertIsNone(lote.vehiculos[0].ruta_archivo)

    def test_sin_datos_se_vuelve_a_consultar_en_la_siguiente_corrida(self):
        # Nada se guardó, así que no es YA_EXISTE: si el dispositivo reportó después, se obtendrá.
        self.correr(mapeo_con(1), SesionLote(archivos={dno(0): GPX_VACIO}))
        lote, sesion = self.correr(mapeo_con(1), SesionLote(archivos={dno(0): GPX_3}))
        self.assertEqual(lote.vehiculos[0].estado, DESCARGADO)
        self.assertEqual(len(sesion.llamadas_exportacion), 1)

    def test_el_mensaje_solo_describe_lo_observado(self):
        lote, _ = self.correr(mapeo_con(1), SesionLote(archivos={dno(0): GPX_VACIO}))
        mensaje = lote.vehiculos[0].mensaje.lower()
        self.assertIn("no entregó posiciones", mensaje)
        for causa in ("avería", "averia", "defect", "desactiv", "apagad", "pérdida", "perdida", "falla"):
            self.assertNotIn(causa, mensaje)

    def test_estado_actual_online_offline_va_aparte_del_resultado_del_periodo(self):
        # ONLINE ahora pero sin datos en el periodo; OFFLINE ahora pero con datos del periodo.
        mapeo = mapeo_con(3, override={0: {"online": True}, 1: {"online": False}, 2: {"online": None}})
        sesion = SesionLote(archivos={dno(0): GPX_VACIO, dno(2): GPX_VACIO})
        lote, _ = self.correr(mapeo, sesion)
        por_llave = {v.llave: v for v in lote.vehiculos}
        self.assertEqual((por_llave[llave(0)].estado, por_llave[llave(0)].estado_actual_baanool),
                         (SIN_DATOS_PERIODO, "ONLINE"))
        self.assertEqual((por_llave[llave(1)].estado, por_llave[llave(1)].estado_actual_baanool),
                         (DESCARGADO, "OFFLINE"))
        self.assertEqual((por_llave[llave(2)].estado, por_llave[llave(2)].estado_actual_baanool),
                         (SIN_DATOS_PERIODO, None))

    def test_gpx_ilegible_sigue_siendo_gpx_invalido_y_se_conserva_para_diagnostico(self):
        sesion = SesionLote(archivos={dno(0): b"<?xml version='1.0'?><gpx><trk><trkpt"})
        lote, _ = self.correr(mapeo_con(1), sesion)
        v = lote.vehiculos[0]
        self.assertEqual(v.estado, GPX_INVALIDO)
        self.assertEqual(len(lote.errores), 1)
        self.assertEqual(sorted(p.name for p in self.carpeta(0).iterdir()), [NOMBRE + ".invalido"])
        self.assertEqual(v.ruta_archivo.name, NOMBRE + ".invalido")

    def test_descarga_correcta_no_deja_temporales(self):
        self.correr(mapeo_con(2))
        for n in range(2):
            self.assertEqual(sorted(p.name for p in self.carpeta(n).iterdir()), [NOMBRE])

    def test_un_punto_sigue_siendo_descargado(self):
        lote, _ = self.correr(mapeo_con(1), SesionLote(archivos={dno(0): GPX_1}))
        self.assertEqual(lote.vehiculos[0].estado, DESCARGADO)

    def test_sin_datos_no_activa_pausa_de_error_ni_detiene(self):
        sesion = SesionLote(archivos={dno(0): GPX_VACIO, dno(1): GPX_VACIO})
        lote, _ = self.correr(mapeo_con(3), sesion)
        self.assertEqual(lote.pendientes, [])
        self.assertEqual(lote.procesados, 3)


if __name__ == "__main__":
    unittest.main()
