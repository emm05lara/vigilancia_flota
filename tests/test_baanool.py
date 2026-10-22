"""Pruebas de src/baanool.py con una sesión HTTP simulada: NO hacen peticiones reales.

El token que se usa aquí es un texto inventado para verificar que nunca se filtra.

    py -m unittest tests.test_baanool -v
"""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import requests

from src import baanool
from src.baanool import (DATA_AUSENTE, DESCARGA_NO_GPX, ERROR_HTTP, ERROR_RED, GPX_INVALIDO, GPX_SIN_PUNTOS,
                         JSON_INVALIDO, PARAMETROS_INVALIDOS, STATUS_BAANOOL, TIMEOUT, TOKEN_AUSENTE,
                         TOKEN_INVALIDO, URL_INVALIDA, ErrorBaanool, descargar_gpx, descargar_gpx_de_baanool,
                         nombre_archivo_local, obtener_token, solicitar_exportacion_gpx)
from src.parametros import TIMEOUT_BAANOOL_SEGUNDOS

TOKEN = "SENTINELA-TOKEN-NO-REAL-0001"
URL_GPX = "https://www.baanooliot.com/bnFile/downloadFileByName?bucktName=icon&fileName=abc123.gpx"
INICIO = datetime(2026, 10, 12, 10, 0, 8)
FIN = datetime(2026, 10, 12, 10, 50, 8)

GPX_VALIDO = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<gpx creator="www.coban.net"><trk><trkseg>
<trkpt lat="19.303938" lon="-99.241882"><ele>0.0</ele><time>2026-10-12 10:00:08</time></trkpt>
<trkpt lat="19.304000" lon="-99.242000"><ele>0.0</ele><time>2026-10-12 10:05:10</time></trkpt>
<trkpt lat="19.310000" lon="-99.250000"><ele>0.0</ele><time>2026-10-12 10:10:12</time></trkpt>
</trkseg></trk></gpx>"""
GPX_SIN_PUNTOS_XML = b'<?xml version="1.0"?><gpx><trk><trkseg></trkseg></trk></gpx>'
HTML_ERROR = b"<html><body>Error 500</body></html>"


class Respuesta:
    def __init__(self, status_code=200, json_data=None, contenido=b"", content_type="application/json",
                 json_invalido=False):
        self.status_code = status_code
        self._json = json_data
        self._json_invalido = json_invalido
        self.content = contenido
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self._json_invalido:
            raise ValueError("no es JSON")
        return self._json


def exito(data=URL_GPX, **extra):
    cuerpo = {"chart": None, "data": data, "length": None, "page": None, "resmsg": "SUCCESS",
              "status": 0, "tips": None, "totalrow": 1}
    cuerpo.update(extra)
    return Respuesta(200, cuerpo)


class Sesion:
    """Reemplaza requests.Session: devuelve las respuestas en orden y registra las llamadas."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.llamadas.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        actual = self.respuestas.pop(0)
        if isinstance(actual, BaseException):
            raise actual
        return actual


def sin_token(prueba, exc):
    prueba.assertNotIn(TOKEN, str(exc))
    prueba.assertNotIn(TOKEN, repr(exc))
    prueba.assertNotIn(TOKEN, exc.mensaje)


class PruebasExportacion(unittest.TestCase):
    def solicitar(self, *respuestas):
        sesion = Sesion(*respuestas)
        return solicitar_exportacion_gpx("DNO123", INICIO, FIN, TOKEN, sesion), sesion

    def test_A_respuesta_correcta(self):
        r, sesion = self.solicitar(exito())
        self.assertEqual((r.http_status, r.status, r.resmsg), (200, 0, "SUCCESS"))
        self.assertEqual(r.url_descarga, URL_GPX)
        self.assertEqual(len(sesion.llamadas), 1)

    def test_B_http_200_con_status_distinto_de_cero(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.solicitar(Respuesta(200, {"status": 1, "resmsg": "param error", "data": None}))
        self.assertEqual(c.exception.codigo, STATUS_BAANOOL)
        self.assertIn("status=1", c.exception.mensaje)
        self.assertIn("param error", c.exception.mensaje)

    def test_C_respuesta_sin_data(self):
        for cuerpo in ({"status": 0, "resmsg": "SUCCESS"}, {"status": 0, "resmsg": "SUCCESS", "data": None},
                       {"status": 0, "resmsg": "SUCCESS", "data": "  "}):
            with self.assertRaises(ErrorBaanool) as c:
                self.solicitar(Respuesta(200, cuerpo))
            self.assertEqual(c.exception.codigo, DATA_AUSENTE)

    def test_D_json_invalido(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.solicitar(Respuesta(200, json_invalido=True, contenido=b"<html>"))
        self.assertEqual(c.exception.codigo, JSON_INVALIDO)
        with self.assertRaises(ErrorBaanool) as c:
            self.solicitar(Respuesta(200, ["no", "es", "objeto"]))
        self.assertEqual(c.exception.codigo, JSON_INVALIDO)

    def test_E_error_http(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.solicitar(Respuesta(500))
        self.assertEqual(c.exception.codigo, ERROR_HTTP)
        self.assertIn("500", c.exception.mensaje)
        for codigo in (401, 403):  # sesión vencida / token inválido
            with self.assertRaises(ErrorBaanool) as c:
                self.solicitar(Respuesta(codigo))
            self.assertEqual(c.exception.codigo, TOKEN_INVALIDO)

    def test_F_timeout_y_error_de_red(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.solicitar(requests.exceptions.ConnectTimeout("tiempo agotado"))
        self.assertEqual(c.exception.codigo, TIMEOUT)
        with self.assertRaises(ErrorBaanool) as c:
            self.solicitar(requests.exceptions.ConnectionError("sin red"))
        self.assertEqual(c.exception.codigo, ERROR_RED)

    def test_K_el_token_se_envia_como_header_y_con_timeout(self):
        _, sesion = self.solicitar(exito())
        llamada = sesion.llamadas[0]
        self.assertEqual(llamada["headers"]["token"], TOKEN)
        self.assertEqual(set(llamada["headers"]), {"token", "Accept"})  # solo los headers mínimos
        self.assertEqual(llamada["timeout"], TIMEOUT_BAANOOL_SEGUNDOS)
        self.assertEqual(llamada["url"], baanool.URL_EXPORTACION)

    def test_L_export_type_2(self):
        _, sesion = self.solicitar(exito())
        self.assertEqual(sesion.llamadas[0]["params"]["exportType"], "2")
        self.assertEqual(sesion.llamadas[0]["params"]["dno"], "DNO123")

    def test_M_formato_de_fechas(self):
        _, sesion = self.solicitar(exito())
        params = sesion.llamadas[0]["params"]
        # Entrada en hora local (2026-10-12 10:00:08 CDMX) -> hora de BAANOOL (+14 h): 2026-10-13 00:00:08.
        self.assertEqual(params["startTime"], "2026-10-13 00:00:08")
        self.assertEqual(params["endTime"], "2026-10-13 00:50:08")
        # requests construye la query (no se codifica a mano).
        url = requests.Request("GET", baanool.URL_EXPORTACION, params=params).prepare().url
        self.assertIn("startTime=2026-10-13+00%3A00%3A08", url)
        self.assertIn("exportType=2", url)
        self.assertNotIn(TOKEN, url)

    def test_parametros_invalidos(self):
        for dno, inicio, fin in (("  ", INICIO, FIN), ("D", FIN, INICIO), ("D", INICIO, INICIO)):
            with self.assertRaises(ErrorBaanool) as c:
                solicitar_exportacion_gpx(dno, inicio, fin, TOKEN, Sesion())
            self.assertEqual(c.exception.codigo, PARAMETROS_INVALIDOS)

    def test_url_de_descarga_ajena_a_baanool_se_rechaza(self):
        for url in ("https://sitio-ajeno.example/archivo.gpx", "https://baanooliot.com.malo.example/a.gpx",
                    "http://www.baanooliot.com/bnFile/x.gpx", "no es una url"):
            with self.assertRaises(ErrorBaanool) as c:
                self.solicitar(exito(data=url))
            self.assertEqual(c.exception.codigo, URL_INVALIDA)


class PruebasDescarga(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.carpeta = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_G_flujo_completo_con_gpx_valido(self):
        sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO, content_type="application/octet-stream"))
        mensajes = []
        r = descargar_gpx_de_baanool("DNO123", INICIO, FIN, self.carpeta, token=TOKEN, sesion=sesion,
                                     avisar=mensajes.append)
        self.assertEqual(r.puntos_validos, 3)
        self.assertEqual(r.ruta, self.carpeta / "DNO123_2026-10-12_100008_105008.gpx")
        self.assertTrue(r.ruta.is_file())
        self.assertEqual(r.ruta.read_bytes(), GPX_VALIDO)  # contenido sin modificar
        self.assertEqual(r.tamano_bytes, len(GPX_VALIDO))
        self.assertEqual(f"{r.primer_timestamp:%H:%M:%S}", "10:00:08")
        self.assertEqual(f"{r.ultimo_timestamp:%H:%M:%S}", "10:10:12")
        self.assertEqual(r.coordenada_inicial, (19.303938, -99.241882))
        # La URL se usa exactamente como la devolvió BAANOOL (con "bucktName") y con el token.
        self.assertEqual(sesion.llamadas[1]["url"], URL_GPX)
        self.assertIn("bucktName=icon", sesion.llamadas[1]["url"])
        self.assertEqual(sesion.llamadas[1]["headers"]["token"], TOKEN)
        self.assertEqual(sesion.llamadas[1]["timeout"], TIMEOUT_BAANOOL_SEGUNDOS)
        self.assertEqual(mensajes, ["Solicitando exportación...", "Exportación preparada.",
                                    "Descargando GPX...", "Archivo guardado.", "Puntos válidos: 3",
                                    "Descarga validada."])
        self.assertFalse(any(TOKEN in m for m in mensajes))

    def test_H_html_o_json_de_error_en_lugar_de_gpx(self):
        for cuerpo, tipo in ((HTML_ERROR, "text/html"), (b'{"status": 1, "resmsg": "expirado"}', "application/json"),
                             (b"", "text/plain")):
            sesion = Sesion(Respuesta(200, contenido=cuerpo, content_type=tipo))
            destino = self.carpeta / "x.gpx"
            with self.assertRaises(ErrorBaanool) as c:
                descargar_gpx(URL_GPX, destino, TOKEN, sesion)
            self.assertEqual(c.exception.codigo, DESCARGA_NO_GPX)
            self.assertFalse(destino.exists())  # no se guarda como si fuera un GPX
        self.assertEqual(list(self.carpeta.iterdir()), [])

    def test_I_gpx_sin_puntos_no_es_descarga_valida(self):
        sesion = Sesion(exito(), Respuesta(200, contenido=GPX_SIN_PUNTOS_XML, content_type="application/xml"))
        with self.assertRaises(ErrorBaanool) as c:
            descargar_gpx_de_baanool("DNO123", INICIO, FIN, self.carpeta, token=TOKEN, sesion=sesion)
        self.assertEqual(c.exception.codigo, GPX_SIN_PUNTOS)
        archivos = sorted(p.name for p in self.carpeta.iterdir())
        self.assertEqual(archivos, ["DNO123_2026-10-12_100008_105008.gpx.invalido"])  # conservado, marcado

    def test_xml_corrupto_es_gpx_invalido(self):
        sesion = Sesion(exito(), Respuesta(200, contenido=b"<?xml version='1.0'?><gpx><trk><trkpt", content_type="a/b"))
        with self.assertRaises(ErrorBaanool) as c:
            descargar_gpx_de_baanool("D1", INICIO, FIN, self.carpeta, token=TOKEN, sesion=sesion)
        self.assertEqual(c.exception.codigo, GPX_INVALIDO)
        self.assertTrue(all(p.name.endswith(".invalido") for p in self.carpeta.iterdir()))

    def test_error_http_y_timeout_en_la_descarga(self):
        with self.assertRaises(ErrorBaanool) as c:
            descargar_gpx(URL_GPX, self.carpeta / "a.gpx", TOKEN, Sesion(Respuesta(404)))
        self.assertEqual(c.exception.codigo, ERROR_HTTP)
        with self.assertRaises(ErrorBaanool) as c:
            descargar_gpx(URL_GPX, self.carpeta / "a.gpx", TOKEN, Sesion(requests.exceptions.ReadTimeout()))
        self.assertEqual(c.exception.codigo, TIMEOUT)
        with self.assertRaises(ErrorBaanool) as c:
            descargar_gpx(URL_GPX, self.carpeta / "a.gpx", TOKEN, Sesion(Respuesta(403)))
        self.assertEqual(c.exception.codigo, TOKEN_INVALIDO)

    def test_descarga_no_envia_el_token_a_un_dominio_ajeno(self):
        sesion = Sesion(Respuesta(200, contenido=GPX_VALIDO))
        with self.assertRaises(ErrorBaanool) as c:
            descargar_gpx("https://sitio-ajeno.example/a.gpx", self.carpeta / "a.gpx", TOKEN, sesion)
        self.assertEqual(c.exception.codigo, URL_INVALIDA)
        self.assertEqual(sesion.llamadas, [])  # ni siquiera se hizo la petición

    def test_J_token_ausente_no_hace_peticiones(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAANOOL_TOKEN", None)
            with self.assertRaises(ErrorBaanool) as c:
                obtener_token()
            self.assertEqual(c.exception.codigo, TOKEN_AUSENTE)
            self.assertIn("BAANOOL_TOKEN", c.exception.mensaje)
            sesion = Sesion(exito())
            with self.assertRaises(ErrorBaanool) as c:
                descargar_gpx_de_baanool("D1", INICIO, FIN, self.carpeta, sesion=sesion)
            self.assertEqual(c.exception.codigo, TOKEN_AUSENTE)
            self.assertEqual(sesion.llamadas, [])
        with mock.patch.dict(os.environ, {"BAANOOL_TOKEN": "   "}):
            with self.assertRaises(ErrorBaanool):
                obtener_token()  # vacío o solo espacios cuenta como ausente

    def test_el_token_se_lee_de_la_variable_de_entorno(self):
        with mock.patch.dict(os.environ, {"BAANOOL_TOKEN": f"  {TOKEN}  "}):
            self.assertEqual(obtener_token(), TOKEN)
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO))
            descargar_gpx_de_baanool("D1", INICIO, FIN, self.carpeta, sesion=sesion)
            self.assertEqual(sesion.llamadas[0]["headers"]["token"], TOKEN)

    def test_N_ningun_error_contiene_el_token(self):
        escenarios = [
            [Respuesta(200, {"status": 1, "resmsg": f"token {TOKEN} expirado", "data": None})],  # el servidor lo repite
            [Respuesta(200, {"status": 401, "resmsg": "x"})],
            [Respuesta(200, {"status": 0, "resmsg": "SUCCESS"})],
            [Respuesta(200, json_invalido=True)],
            [Respuesta(500)],
            [Respuesta(401)],
            [requests.exceptions.Timeout()],
            [requests.exceptions.ConnectionError(f"fallo con {TOKEN}")],
            [exito(), Respuesta(200, contenido=HTML_ERROR, content_type="text/html")],
            [exito(), Respuesta(404)],
            [exito(), requests.exceptions.ReadTimeout()],
            [exito(), Respuesta(200, contenido=GPX_SIN_PUNTOS_XML)],
            [exito(), Respuesta(200, contenido=b"<?xml version='1.0'?><gpx><trkpt")],
            [exito(data="https://ajeno.example/a.gpx")],
        ]
        for respuestas in escenarios:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ErrorBaanool) as c:
                    descargar_gpx_de_baanool("D1", INICIO, FIN, Path(tmp), token=TOKEN, sesion=Sesion(*respuestas))
                sin_token(self, c.exception)

    def test_nombre_local_saneado(self):
        self.assertEqual(nombre_archivo_local("DNO123", INICIO, FIN), "DNO123_2026-10-12_100008_105008.gpx")
        self.assertEqual(nombre_archivo_local("a/b:c*?", INICIO, FIN), "a_b_c___2026-10-12_100008_105008.gpx")
        otro_dia = datetime(2026, 10, 13, 1, 0, 0)
        self.assertEqual(nombre_archivo_local("D1", INICIO, otro_dia), "D1_2026-10-12_100008_2026-10-13_010000.gpx")


class PruebasRefererYEstados(unittest.TestCase):
    """Prueba controlada de una sola variable: el header Referer (más status 105 y 5550)."""

    LAT, LNG = 19.405906, -99.113198

    def exportar(self, *respuestas, con_coordenadas=True):
        sesion = Sesion(*respuestas)
        kwargs = {"latitud": self.LAT, "longitud": self.LNG} if con_coordenadas else {}
        r = solicitar_exportacion_gpx("864035054122222", INICIO, FIN, TOKEN, sesion, **kwargs)
        return r, sesion

    def test_A_referer_generado_con_dno_lng_y_lat(self):
        url = baanool.construir_referer("864035054122222", self.LAT, self.LNG)
        self.assertEqual(url, "https://www.baanooliot.com/playGoogle?dno=864035054122222"
                              "&lng=-99.113198&lat=19.405906")

    def test_A2_el_referer_se_escapa_de_forma_estandar(self):
        url = baanool.construir_referer("a b&c=d", 1.5, -2.5)
        self.assertIn("dno=a+b%26c%3Dd", url)  # urlencode: nada se concatena sin escapar
        self.assertNotIn("&c=d", url)

    def test_A3_coordenadas_invalidas(self):
        for lat, lng in ((91, 0), (0, 181), (None, 0), ("19.4", 0), (True, 0)):
            with self.assertRaises(ErrorBaanool) as c:
                baanool.construir_referer("D1", lat, lng)
            self.assertEqual(c.exception.codigo, PARAMETROS_INVALIDOS)
        with self.assertRaises(ErrorBaanool) as c:  # solo una de las dos: no se hace la petición
            sesion = Sesion(exito())
            solicitar_exportacion_gpx("D1", INICIO, FIN, TOKEN, sesion, latitud=19.4)
        self.assertEqual(c.exception.codigo, PARAMETROS_INVALIDOS)

    def test_B_referer_se_envia_cuando_hay_lat_lng(self):
        _, sesion = self.exportar(exito())
        headers = sesion.llamadas[0]["headers"]
        self.assertEqual(headers["Referer"], "https://www.baanooliot.com/playGoogle?dno=864035054122222"
                                             "&lng=-99.113198&lat=19.405906")
        self.assertEqual(set(headers), {"token", "Accept", "Referer"})
        self.assertEqual(headers["token"], TOKEN)

    def test_C_sin_lat_lng_no_se_envia_referer(self):
        _, sesion = self.exportar(exito(), con_coordenadas=False)
        headers = sesion.llamadas[0]["headers"]
        self.assertNotIn("Referer", headers)
        self.assertEqual(set(headers), {"token", "Accept"})

    def test_D_nunca_se_envian_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO, content_type="application/xml"))
            descargar_gpx_de_baanool("864035054122222", INICIO, FIN, Path(tmp), token=TOKEN, sesion=sesion,
                                     latitud=self.LAT, longitud=self.LNG)
        for llamada in sesion.llamadas:
            self.assertFalse({h.lower() for h in llamada["headers"]} & {"cookie", "set-cookie"})

    def test_D2_la_sesion_real_no_reenvia_cookies_que_el_servidor_establece(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Manejador(BaseHTTPRequestHandler):
            def do_GET(self):
                cuerpo = json.dumps({"cookie": self.headers.get("Cookie")}).encode()
                self.send_response(200)
                if self.path == "/set":
                    self.send_header("Set-Cookie", "sid=abc123; Path=/")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)

            def log_message(self, *args):
                pass

        servidor = HTTPServer(("127.0.0.1", 0), Manejador)
        hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
        hilo.start()
        try:
            base = f"http://127.0.0.1:{servidor.server_port}"
            control = requests.Session()  # sesión normal: sí reenvía la cookie (valida la prueba)
            control.trust_env = False
            control.get(base + "/set", timeout=5)
            self.assertEqual(control.get(base + "/echo", timeout=5).json()["cookie"], "sid=abc123")

            nuestra = baanool.crear_sesion()
            nuestra.trust_env = False
            nuestra.get(base + "/set", timeout=5)
            self.assertIsNone(nuestra.get(base + "/echo", timeout=5).json()["cookie"])
        finally:
            servidor.shutdown()
            servidor.server_close()

    def test_E_no_se_agregan_headers_sec_ni_otros(self):
        _, sesion = self.exportar(exito())
        nombres = {h.lower() for h in sesion.llamadas[0]["headers"]}
        self.assertFalse([h for h in nombres if h.startswith("sec-")])
        self.assertFalse(nombres & {"cookie", "origin", "user-agent", "accept-language", "authorization"})

    def test_la_descarga_no_lleva_referer(self):
        # Solo cambia la solicitud de exportación; la descarga conserva token + Accept.
        sesion = Sesion(Respuesta(200, contenido=GPX_VALIDO))
        with tempfile.TemporaryDirectory() as tmp:
            descargar_gpx(URL_GPX, Path(tmp) / "a.gpx", TOKEN, sesion)
        self.assertEqual(set(sesion.llamadas[0]["headers"]), {"token", "Accept"})

    def test_F_status_5550_es_rate_limit(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.exportar(Respuesta(200, {"status": 5550, "resmsg": "Too many requests", "data": None}))
        self.assertEqual(c.exception.codigo, baanool.RATE_LIMIT_BAANOOL)
        self.assertEqual(c.exception.mensaje, "BAANOOL limitó temporalmente las solicitudes. "
                                              "Espera antes de volver a intentar.")

    def test_F2_5550_no_reintenta_ni_espera(self):
        sesion = Sesion(Respuesta(200, {"status": 5550, "resmsg": "Too many requests"}), exito())
        with self.assertRaises(ErrorBaanool):
            solicitar_exportacion_gpx("D1", INICIO, FIN, TOKEN, sesion)
        self.assertEqual(len(sesion.llamadas), 1)  # una sola petición: sin retry automático

    def test_G_status_105_es_permiso_no_token_invalido(self):
        resmsg = "暂无权限"
        with self.assertRaises(ErrorBaanool) as c:
            self.exportar(Respuesta(200, {"status": 105, "resmsg": resmsg, "data": None}))
        self.assertEqual(c.exception.codigo, baanool.PERMISO_BAANOOL)
        self.assertNotEqual(c.exception.codigo, TOKEN_INVALIDO)
        self.assertIn("status=105", c.exception.mensaje)
        self.assertIn(resmsg, c.exception.mensaje)
        self.assertIn("sesión vencida", c.exception.mensaje)  # aclara que NO se asume sesión vencida
        str(c.exception).encode("utf-8")  # el texto chino no rompe la construcción del mensaje

    def test_H_el_token_no_aparece_en_los_nuevos_mensajes(self):
        for cuerpo in ({"status": 5550, "resmsg": f"Too many requests {TOKEN}"},
                       {"status": 105, "resmsg": f"暂无权限 {TOKEN}"}):
            with self.assertRaises(ErrorBaanool) as c:
                self.exportar(Respuesta(200, cuerpo))
            sin_token(self, c.exception)
            self.assertNotIn(TOKEN, c.exception.mensaje)
        referer = baanool.construir_referer("864035054122222", self.LAT, self.LNG)
        self.assertNotIn(TOKEN, referer)

    def test_flujo_completo_con_referer_en_la_exportacion(self):
        with tempfile.TemporaryDirectory() as tmp:
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO))
            r = descargar_gpx_de_baanool("864035054122222", INICIO, FIN, Path(tmp), token=TOKEN, sesion=sesion,
                                         latitud=self.LAT, longitud=self.LNG)
        self.assertEqual(r.puntos_validos, 3)
        self.assertIn("Referer", sesion.llamadas[0]["headers"])
        self.assertNotIn("Referer", sesion.llamadas[1]["headers"])


class PruebasUserAgent(unittest.TestCase):
    """Segunda prueba controlada: se agrega SOLO User-Agent (sobre token, Accept y Referer)."""

    LAT, LNG = 19.405906, -99.113198
    UA = baanool.USER_AGENT_BAANOOL

    def exportar(self, *respuestas, con_coordenadas=True, con_user_agent=True):
        sesion = Sesion(*respuestas)
        kwargs = {}
        if con_coordenadas:
            kwargs.update(latitud=self.LAT, longitud=self.LNG)
        if con_user_agent:
            kwargs["user_agent"] = self.UA
        r = solicitar_exportacion_gpx("864035054122222", INICIO, FIN, TOKEN, sesion, **kwargs)
        return r, sesion

    def headers(self, **kwargs):
        _, sesion = self.exportar(exito(), **kwargs)
        return sesion.llamadas[0]["headers"]

    def test_constante_del_user_agent(self):
        self.assertEqual(self.UA, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")

    def test_1_token_sigue_presente(self):
        self.assertEqual(self.headers()["token"], TOKEN)

    def test_2_accept_sigue_presente(self):
        self.assertEqual(self.headers()["Accept"], "application/json, text/plain, */*")

    def test_3_referer_sigue_presente_con_lat_lng(self):
        self.assertEqual(self.headers()["Referer"], "https://www.baanooliot.com/playGoogle?"
                                                    "dno=864035054122222&lng=-99.113198&lat=19.405906")
        self.assertNotIn("Referer", self.headers(con_coordenadas=False))

    def test_4_user_agent_se_envia_correctamente(self):
        h = self.headers()
        self.assertEqual(h["User-Agent"], self.UA)
        self.assertEqual(set(h), {"token", "Accept", "Referer", "User-Agent"})  # y nada más

    def test_4b_sin_user_agent_no_se_envia(self):
        self.assertNotIn("User-Agent", self.headers(con_user_agent=False))
        self.assertEqual(set(self.headers(con_user_agent=False)), {"token", "Accept", "Referer"})

    def test_5_no_se_envian_cookies(self):
        self.assertNotIn("cookie", {h.lower() for h in self.headers()})
        with tempfile.TemporaryDirectory() as tmp:
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO))
            descargar_gpx_de_baanool("864035054122222", INICIO, FIN, Path(tmp), token=TOKEN, sesion=sesion,
                                     latitud=self.LAT, longitud=self.LNG, user_agent=self.UA)
        for llamada in sesion.llamadas:
            self.assertNotIn("cookie", {h.lower() for h in llamada["headers"]})
        # La sesión real sigue rechazando cookies.
        self.assertEqual(list(baanool.crear_sesion().cookies), [])

    def test_6_no_se_agregan_sec_ni_otros_headers(self):
        nombres = {h.lower() for h in self.headers()}
        self.assertFalse([h for h in nombres if h.startswith("sec-")])
        self.assertFalse(nombres & {"origin", "accept-language", "sec-ch-ua", "authorization", "cookie"})

    def test_user_agent_solo_en_la_exportacion_no_en_la_descarga(self):
        with tempfile.TemporaryDirectory() as tmp:
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO))
            descargar_gpx_de_baanool("864035054122222", INICIO, FIN, Path(tmp), token=TOKEN, sesion=sesion,
                                     latitud=self.LAT, longitud=self.LNG, user_agent=self.UA)
        self.assertEqual(sesion.llamadas[0]["headers"]["User-Agent"], self.UA)
        self.assertEqual(set(sesion.llamadas[1]["headers"]), {"token", "Accept"})

    def test_7_status_105_sigue_siendo_permiso(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.exportar(Respuesta(200, {"status": 105, "resmsg": "暂无权限", "data": None}))
        self.assertEqual(c.exception.codigo, baanool.PERMISO_BAANOOL)
        self.assertNotEqual(c.exception.codigo, TOKEN_INVALIDO)

    def test_8_status_5550_sigue_siendo_rate_limit(self):
        with self.assertRaises(ErrorBaanool) as c:
            self.exportar(Respuesta(200, {"status": 5550, "resmsg": "Too many requests", "data": None}))
        self.assertEqual(c.exception.codigo, baanool.RATE_LIMIT_BAANOOL)

    def test_9_ningun_error_expone_el_token(self):
        escenarios = [
            Respuesta(200, {"status": 105, "resmsg": f"暂无 {TOKEN}"}),
            Respuesta(200, {"status": 5550, "resmsg": f"Too many {TOKEN}"}),
            Respuesta(200, {"status": 1, "resmsg": f"fallo {TOKEN}"}),
            Respuesta(200, {"status": 0, "resmsg": "SUCCESS"}),
            Respuesta(200, json_invalido=True),
            Respuesta(401),
            Respuesta(500),
            requests.exceptions.Timeout(f"agotado {TOKEN}"),
            requests.exceptions.ConnectionError(f"fallo {TOKEN}"),
        ]
        for respuesta in escenarios:
            with self.assertRaises(ErrorBaanool) as c:
                self.exportar(respuesta)
            sin_token(self, c.exception)
        # El User-Agent y el Referer tampoco llevan el token.
        h = self.headers()
        self.assertNotIn(TOKEN, h["User-Agent"] + h["Referer"])


class PruebasZonaHoraria(unittest.TestCase):
    """startTime/endTime se envían en hora de BAANOOL (Asia/Shanghai); el resto no se convierte."""

    def conv(self, texto):
        local = datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")
        return baanool.convertir_fecha_local_a_baanool(local).strftime("%Y-%m-%d %H:%M:%S")

    def test_A_ejemplo_validado_inicio(self):
        self.assertEqual(self.conv("2026-10-10 20:00:00"), "2026-10-11 10:00:00")

    def test_B_ejemplo_validado_fin(self):
        self.assertEqual(self.conv("2026-10-11 21:00:00"), "2026-10-12 11:00:00")

    def test_C_cambio_de_dia(self):
        self.assertEqual(self.conv("2026-10-10 20:00:00"), "2026-10-11 10:00:00")
        self.assertEqual(self.conv("2026-10-10 10:00:00"), "2026-10-11 00:00:00")  # justo a medianoche
        self.assertEqual(self.conv("2026-10-10 09:59:59"), "2026-10-10 23:59:59")  # sin cambio de día

    def test_D_cambio_de_mes(self):
        self.assertEqual(self.conv("2026-10-31 20:00:00"), "2026-11-01 10:00:00")
        self.assertEqual(self.conv("2028-02-28 20:00:00"), "2028-02-29 10:00:00")  # año bisiesto

    def test_E_cambio_de_anio(self):
        self.assertEqual(self.conv("2026-12-31 20:00:00"), "2027-01-01 10:00:00")

    def test_usa_zoneinfo_no_un_desfase_fijo(self):
        # En verano de 2021 México estaba en UTC-5 (horario de verano): el desfase con UTC+8 era 13 h.
        self.assertEqual(self.conv("2021-07-01 12:00:00"), "2021-07-02 01:00:00")
        # En invierno (UTC-6) son 14 h.
        self.assertEqual(self.conv("2021-12-01 12:00:00"), "2021-12-02 02:00:00")

    def test_fecha_con_zona_horaria_se_convierte_desde_su_zona(self):
        from datetime import timezone
        utc = datetime(2026, 10, 11, 2, 0, 0, tzinfo=timezone.utc)  # = 20:00 del 10-oct en CDMX
        self.assertEqual(baanool.convertir_fecha_local_a_baanool(utc).replace(tzinfo=None),
                         datetime(2026, 10, 11, 10, 0, 0))
        self.assertIsNone(baanool.convertir_fecha_local_a_baanool(utc).tzinfo)  # sin zona al formatear

    def test_F_inicio_mayor_o_igual_a_fin_sigue_rechazandose(self):
        for inicio, fin in ((FIN, INICIO), (INICIO, INICIO)):
            sesion = Sesion(exito())
            with self.assertRaises(ErrorBaanool) as c:
                solicitar_exportacion_gpx("D1", inicio, fin, TOKEN, sesion)
            self.assertEqual(c.exception.codigo, PARAMETROS_INVALIDOS)
            self.assertEqual(sesion.llamadas, [])

    def test_G_los_params_enviados_contienen_las_fechas_convertidas(self):
        sesion = Sesion(exito())
        r = solicitar_exportacion_gpx("864035054122222", datetime(2026, 10, 10, 20, 0, 0),
                                      datetime(2026, 10, 11, 21, 0, 0), TOKEN, sesion)
        params = sesion.llamadas[0]["params"]
        self.assertEqual(params["startTime"], "2026-10-11 10:00:00")
        self.assertEqual(params["endTime"], "2026-10-12 11:00:00")
        self.assertEqual((r.inicio_enviado, r.fin_enviado), ("2026-10-11 10:00:00", "2026-10-12 11:00:00"))
        self.assertEqual(params["exportType"], "2")

    def test_H_el_nombre_de_archivo_usa_las_fechas_locales_originales(self):
        inicio, fin = datetime(2026, 10, 10, 20, 0, 0), datetime(2026, 10, 11, 21, 0, 0)
        self.assertEqual(nombre_archivo_local("864035054122222", inicio, fin),
                         "864035054122222_2026-10-10_200000_2026-10-11_210000.gpx")
        with tempfile.TemporaryDirectory() as tmp:
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO))
            r = descargar_gpx_de_baanool("864035054122222", inicio, fin, Path(tmp), token=TOKEN, sesion=sesion)
            self.assertEqual(r.ruta.name, "864035054122222_2026-10-10_200000_2026-10-11_210000.gpx")
            self.assertTrue(r.ruta.is_file())
        self.assertNotIn("2026-10-11_100000", r.ruta.name)  # no lleva la hora de BAANOOL

    def test_I_los_timestamps_del_gpx_no_se_modifican(self):
        inicio, fin = datetime(2026, 10, 12, 9, 0, 0), datetime(2026, 10, 12, 11, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            sesion = Sesion(exito(), Respuesta(200, contenido=GPX_VALIDO))
            r = descargar_gpx_de_baanool("D1", inicio, fin, Path(tmp), token=TOKEN, sesion=sesion)
            # La petición salió convertida (+14 h)...
            self.assertEqual(sesion.llamadas[0]["params"]["startTime"], "2026-10-12 23:00:00")
            # ...pero el archivo y sus timestamps quedan exactamente como los entregó BAANOOL.
            self.assertEqual(r.ruta.read_bytes(), GPX_VALIDO)
            self.assertEqual(f"{r.primer_timestamp:%Y-%m-%d %H:%M:%S}", "2026-10-12 10:00:08")
            self.assertEqual(f"{r.ultimo_timestamp:%Y-%m-%d %H:%M:%S}", "2026-10-12 10:10:12")

    def test_J_el_token_sigue_protegido(self):
        escenarios = [
            [Respuesta(200, {"status": 1, "resmsg": f"error {TOKEN}"})],
            [Respuesta(200, {"status": 105, "resmsg": TOKEN})],
            [Respuesta(200, {"status": 5550, "resmsg": TOKEN})],
            [exito(), Respuesta(200, contenido=HTML_ERROR, content_type="text/html")],
            [requests.exceptions.Timeout(TOKEN)],
        ]
        for respuestas in escenarios:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ErrorBaanool) as c:
                    descargar_gpx_de_baanool("D1", datetime(2026, 10, 10, 20, 0, 0), datetime(2026, 10, 11, 21, 0, 0),
                                             Path(tmp), token=TOKEN, sesion=Sesion(*respuestas))
                sin_token(self, c.exception)


if __name__ == "__main__":
    unittest.main()
