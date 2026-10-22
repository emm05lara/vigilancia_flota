"""Pruebas de la consulta de dispositivos de BAANOOL y del mapeo LLAVE <-> dispositivo.

La sesión HTTP es simulada: NO se hace ninguna llamada real. El token es un texto inventado.

    py -m unittest tests.test_dispositivos_baanool -v
"""

import unittest

import requests

from src import baanool
from src.baanool import (DATA_AUSENTE, DISPOSITIVOS_AUSENTES, DISPOSITIVOS_VACIOS, DNO_INVALIDO, DNO_VACIO,
                         ERROR_HTTP, ERROR_RED, JSON_INVALIDO, NAME_VACIO, STATUS_BAANOOL, TIMEOUT,
                         TOKEN_INVALIDO, USER_AGENT_BAANOOL, ErrorBaanool, obtener_dispositivos)
from src.mapeo_baanool import (COINCIDENCIA_EXACTA, CONFLICTO, POSIBLE_COINCIDENCIA, SOLO_BAANOOL, SOLO_LOCAL,
                               mapear_llaves_con_dispositivos)
from src.parametros import TIMEOUT_BAANOOL_SEGUNDOS

TOKEN = "SENTINELA-TOKEN-NO-REAL-0002"
L1, L2, L3 = "AVEO|2022|AZUL|PCH9635", "AVEO|2018|ARENA|F86AYT", "VENTO|2019|BLANCO|LUE965C"


def dispositivo(name, dno, car_num=None, **extra):
    d = {"name": name, "dno": dno, "carNum": car_num, "lat": 19.4, "lng": -99.1, "devStatus": 1, "online": True,
         "groupId": 7, "timeZone": "GMT-6", "mileage": 123.4, "gsm": 20, "gps": 12}
    d.update(extra)
    return d


def respuesta_ok(*dispositivos):
    return Respuesta(200, {"chart": None, "data": [{"devices": list(dispositivos), "groups": []}],
                           "resmsg": "SUCCESS", "status": 0, "totalrow": 1})


class Respuesta:
    def __init__(self, status_code=200, json_data=None, json_invalido=False):
        self.status_code = status_code
        self._json = json_data
        self._json_invalido = json_invalido
        self.content = b""
        self.headers = {}

    def json(self):
        if self._json_invalido:
            raise ValueError("no es JSON")
        return self._json


class Sesion:
    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def _atender(self, metodo, url, **kwargs):
        self.llamadas.append({"metodo": metodo, "url": url, **kwargs})
        actual = self.respuestas.pop(0)
        if isinstance(actual, BaseException):
            raise actual
        return actual

    def post(self, url, data=None, headers=None, timeout=None, **kwargs):
        return self._atender("POST", url, data=data, headers=headers, timeout=timeout)

    def get(self, url, **kwargs):  # no debe usarse en esta consulta
        return self._atender("GET", url, **kwargs)


def consultar(*respuestas):
    sesion = Sesion(*respuestas)
    return obtener_dispositivos(TOKEN, sesion), sesion


def sin_token(prueba, exc):
    prueba.assertNotIn(TOKEN, str(exc))
    prueba.assertNotIn(TOKEN, repr(exc))
    prueba.assertNotIn(TOKEN, exc.mensaje)


class PruebasConsulta(unittest.TestCase):
    def test_A_respuesta_valida_con_varios_dispositivos(self):
        r, sesion = consultar(respuesta_ok(dispositivo(L1, 864035054122222, "PCH9635"),
                                           dispositivo(L2, "864035054133333", "F86AYT"),
                                           dispositivo(L3, "864035054144444", "LUE965C", online=False)))
        self.assertEqual((r.http_status, r.status, r.resmsg), (200, 0, "SUCCESS"))
        self.assertEqual([d.name for d in r.dispositivos], [L1, L2, L3])
        self.assertEqual(r.advertencias, [])
        self.assertEqual(len(sesion.llamadas), 1)
        self.assertEqual(sesion.llamadas[0]["metodo"], "POST")  # POST, no GET

    def test_B_status_distinto_de_cero(self):
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(200, {"status": 1, "resmsg": "fallo", "data": None}))
        self.assertEqual(c.exception.codigo, STATUS_BAANOOL)

    def test_B2_105_y_5550_siguen_mapeados(self):
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(200, {"status": 105, "resmsg": "暂无权限"}))
        self.assertEqual(c.exception.codigo, baanool.PERMISO_BAANOOL)
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(200, {"status": 5550, "resmsg": "Too many requests"}))
        self.assertEqual(c.exception.codigo, baanool.RATE_LIMIT_BAANOOL)

    def test_C_devices_ausente(self):
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(200, {"status": 0, "resmsg": "SUCCESS", "data": [{"groups": []}]}))
        self.assertEqual(c.exception.codigo, DISPOSITIVOS_AUSENTES)
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(200, {"status": 0, "resmsg": "SUCCESS", "data": None}))
        self.assertEqual(c.exception.codigo, DATA_AUSENTE)

    def test_D_devices_vacio(self):
        with self.assertRaises(ErrorBaanool) as c:
            consultar(respuesta_ok())
        self.assertEqual(c.exception.codigo, DISPOSITIVOS_VACIOS)

    def test_E_dispositivo_con_name_y_dno_validos(self):
        r, _ = consultar(respuesta_ok(dispositivo(L1, "864035054122222", "PCH9635", lat=19.405906, lng=-99.113198)))
        d = r.dispositivos[0]
        self.assertEqual((d.name, d.dno, d.car_num), (L1, "864035054122222", "PCH9635"))
        self.assertEqual((d.lat, d.lng), (19.405906, -99.113198))
        self.assertTrue(d.utilizable)
        self.assertIs(d.online, True)
        self.assertEqual(d.dev_status, 1)

    def test_F_dno_se_trata_como_texto(self):
        r, _ = consultar(respuesta_ok(dispositivo(L1, 864035054122222), dispositivo(L2, "  000123456789012 "),
                                      dispositivo(L3, 8.64035054122222e14)))
        self.assertEqual(r.dispositivos[0].dno, "864035054122222")
        self.assertIsInstance(r.dispositivos[0].dno, str)
        self.assertEqual(r.dispositivos[1].dno, "000123456789012")  # ceros conservados, espacios externos fuera
        self.assertIsNone(r.dispositivos[2].dno)  # un decimal no se acepta como identificador
        self.assertIn(DNO_INVALIDO, r.dispositivos[2].problemas)

    def test_G_dispositivo_sin_name_no_tira_la_lista(self):
        r, _ = consultar(respuesta_ok(dispositivo(L1, "864035054122222"), dispositivo(None, "864035054133333"),
                                      dispositivo("   ", "864035054144444")))
        self.assertEqual(len(r.dispositivos), 3)
        self.assertIn(NAME_VACIO, r.dispositivos[1].problemas)
        self.assertIn(NAME_VACIO, r.dispositivos[2].problemas)
        self.assertTrue(r.dispositivos[0].utilizable)

    def test_H_dispositivo_sin_dno(self):
        r, _ = consultar(respuesta_ok(dispositivo(L1, None), dispositivo(L2, ""), dispositivo(L3, "86 40")))
        self.assertEqual([DNO_VACIO in d.problemas for d in r.dispositivos], [True, True, False])
        self.assertIn(DNO_INVALIDO, r.dispositivos[2].problemas)  # espacio interno: no se corrige
        self.assertTrue(all(d.dno is None for d in r.dispositivos))

    def test_elemento_que_no_es_objeto_se_registra_como_advertencia(self):
        r, _ = consultar(respuesta_ok(dispositivo(L1, "864035054122222"), "basura", 42, None))
        self.assertEqual(len(r.dispositivos), 1)
        self.assertEqual(len(r.advertencias), 3)
        self.assertEqual(r.dispositivos[0].indice, 0)

    def test_campos_opcionales_mal_formados_no_rompen(self):
        r, _ = consultar(respuesta_ok(dispositivo(L1, "864035054122222", lat="x", lng=None, online="quizá",
                                                  carNum=123)))
        d = r.dispositivos[0]
        self.assertEqual((d.lat, d.lng, d.online, d.car_num), (None, None, None, None))
        self.assertTrue(d.utilizable)  # lat/lng/online no son críticos

    def test_json_invalido_http_y_red(self):
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(200, json_invalido=True))
        self.assertEqual(c.exception.codigo, JSON_INVALIDO)
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(500))
        self.assertEqual(c.exception.codigo, ERROR_HTTP)
        with self.assertRaises(ErrorBaanool) as c:
            consultar(Respuesta(401))
        self.assertEqual(c.exception.codigo, TOKEN_INVALIDO)
        with self.assertRaises(ErrorBaanool) as c:
            consultar(requests.exceptions.ConnectTimeout())
        self.assertEqual(c.exception.codigo, TIMEOUT)
        with self.assertRaises(ErrorBaanool) as c:
            consultar(requests.exceptions.ConnectionError())
        self.assertEqual(c.exception.codigo, ERROR_RED)

    # ---------- headers y petición (P-T) ----------

    def llamada(self):
        _, sesion = consultar(respuesta_ok(dispositivo(L1, "864035054122222")))
        return sesion.llamadas[0]

    def test_P_el_token_se_envia(self):
        self.assertEqual(self.llamada()["headers"]["token"], TOKEN)

    def test_Q_origin(self):
        self.assertEqual(self.llamada()["headers"]["Origin"], "https://www.baanooliot.com")

    def test_R_referer(self):
        self.assertEqual(self.llamada()["headers"]["Referer"], "https://www.baanooliot.com/")

    def test_S_user_agent(self):
        self.assertEqual(self.llamada()["headers"]["User-Agent"], USER_AGENT_BAANOOL)

    def test_T_sin_cookies_y_sin_otros_headers(self):
        h = self.llamada()["headers"]
        self.assertEqual(set(h), {"token", "Accept", "Content-Type", "Origin", "Referer", "User-Agent"})
        self.assertFalse([k for k in h if k.lower() in ("cookie", "accept-language", "authorization")
                          or k.lower().startswith("sec-")])
        self.assertEqual(h["Content-Type"], "application/x-www-form-urlencoded;charset=UTF-8")
        self.assertEqual(h["Accept"], "application/json, text/plain, */*")

    def test_peticion_post_con_cuerpo_vacio_url_y_timeout(self):
        ll = self.llamada()
        self.assertEqual(ll["metodo"], "POST")
        self.assertEqual(ll["url"], "https://www.baanooliot.com/car-api/bnclw-onboard/personController/"
                                    "getMyDeviceListData")
        self.assertEqual(ll["data"], b"")
        self.assertEqual(ll["timeout"], TIMEOUT_BAANOOL_SEGUNDOS)

    def test_U_ningun_error_expone_el_token(self):
        escenarios = [
            Respuesta(200, {"status": 1, "resmsg": f"fallo {TOKEN}"}),
            Respuesta(200, {"status": 105, "resmsg": f"暂 {TOKEN}"}),
            Respuesta(200, {"status": 5550, "resmsg": TOKEN}),
            Respuesta(200, {"status": 0, "data": None}),
            Respuesta(200, {"status": 0, "data": [{}]}),
            respuesta_ok(),
            Respuesta(200, json_invalido=True),
            Respuesta(401), Respuesta(500),
            requests.exceptions.Timeout(f"agotado {TOKEN}"),
            requests.exceptions.ConnectionError(f"fallo {TOKEN}"),
        ]
        for respuesta in escenarios:
            with self.assertRaises(ErrorBaanool) as c:
                consultar(respuesta)
            sin_token(self, c.exception)


def dev(indice, name, dno, car_num=None, **kw):
    return baanool._convertir_dispositivo(dispositivo(name, dno, car_num, **kw), indice)


def mapear(llaves, *dispositivos):
    return mapear_llaves_con_dispositivos(llaves, [dev(i, *d) if not isinstance(d, dict) else
                                                   baanool._convertir_dispositivo(d, i)
                                                   for i, d in enumerate(dispositivos)])


class PruebasMapeo(unittest.TestCase):
    def entrada(self, resultado, llave=None, estado=None):
        coincidentes = [e for e in resultado.entradas if (llave is None or e.llave == llave)
                        and (estado is None or e.estado == estado)]
        self.assertEqual(len(coincidentes), 1, [(e.estado, e.llave) for e in resultado.entradas])
        return coincidentes[0]

    def test_K_llave_igual_a_name_es_coincidencia_exacta(self):
        r = mapear([L1], (L1, 864035054122222, "PCH9635", ))
        e = self.entrada(r, L1)
        self.assertEqual(e.estado, COINCIDENCIA_EXACTA)
        self.assertEqual(e.dno, "864035054122222")
        self.assertEqual((e.dispositivo.car_num, e.dispositivo.lat, e.dispositivo.lng), ("PCH9635", 19.4, -99.1))
        self.assertEqual(r.exactas, {L1: e.dispositivo})

    def test_L_llave_sin_name_es_solo_local(self):
        r = mapear([L1, L2], (L1, "864035054122222"))
        e = self.entrada(r, L2)
        self.assertEqual(e.estado, SOLO_LOCAL)
        self.assertIsNone(e.dno)  # no se inventa ningún DNO
        self.assertEqual(e.dispositivos, ())

    def test_M_name_sin_llave_es_solo_baanool(self):
        r = mapear([L1], (L1, "864035054122222"), (L3, "864035054144444"))
        e = self.entrada(r, estado=SOLO_BAANOOL)
        self.assertEqual(e.dispositivos[0].name, L3)
        self.assertEqual(r.locales, [L1])  # no se crea ninguna LLAVE nueva
        self.assertEqual(set(r.exactas), {L1})

    def test_N_diferencia_de_separador_es_posible_no_exacta(self):
        r = mapear(["AVEO/2026/BLANCO/NJG463C"], ("AVEO|2026|BLANCO|NJG463C", "864035054155555", "NJG463C"))
        e = self.entrada(r, "AVEO/2026/BLANCO/NJG463C")
        self.assertEqual(e.estado, POSIBLE_COINCIDENCIA)
        self.assertIn("separador", e.motivo)
        self.assertIsNone(e.dno)  # nunca se asigna automáticamente
        self.assertIsNone(e.dispositivo)
        self.assertEqual(r.exactas, {})
        self.assertEqual(r.con_estado(SOLO_BAANOOL), [])  # el dispositivo aparece como candidato, no duplicado

    def test_O_carnum_coincide_con_la_placa_final_es_solo_diagnostico(self):
        r = mapear(["AVEO|2024|GRIS|ABC123Z"], ("OTRO|FORMATO|DISTINTO", "864035054166666", "abc123z"))
        e = self.entrada(r, "AVEO|2024|GRIS|ABC123Z")
        self.assertEqual(e.estado, POSIBLE_COINCIDENCIA)
        self.assertIn("placa", e.motivo)
        self.assertIsNone(e.dno)
        self.assertEqual(r.exactas, {})

    def test_mayusculas_y_espacios_son_posibles_no_exactos(self):
        r = mapear(["aveo|2022|azul|pch9635", "AVEO | 2018 | ARENA | F86AYT"], (L1, "864035054122222"),
                   (L2, "864035054133333"))
        for e in r.entradas:
            self.assertEqual(e.estado, POSIBLE_COINCIDENCIA)
        self.assertIn("mayúsculas", r.entradas[0].motivo)
        self.assertIn("espacios", r.entradas[1].motivo)
        self.assertEqual(r.exactas, {})

    def test_sin_parecido_no_hay_posible(self):
        r = mapear(["AVEO|2022|AZUL|PCH9635"], ("AVEO|2022|ROJO|XYZ789", "864035054122222", "XYZ789"))
        self.assertEqual(self.entrada(r, "AVEO|2022|AZUL|PCH9635").estado, SOLO_LOCAL)
        self.assertEqual(len(r.con_estado(SOLO_BAANOOL)), 1)

    def test_I_dos_dispositivos_con_el_mismo_name(self):
        r = mapear([L1], (L1, "864035054122222"), (L1, "864035054133333"))
        e = self.entrada(r, L1)
        self.assertEqual(e.estado, CONFLICTO)
        self.assertIn("name duplicado", e.motivo)
        self.assertIsNone(e.dno)  # no se elige ninguno
        self.assertEqual(len(e.dispositivos), 2)
        self.assertEqual(len(r.entradas), 1)  # no aparecen además como SOLO_BAANOOL

    def test_J_dos_dispositivos_con_el_mismo_dno(self):
        r = mapear([L1, L2], (L1, "864035054122222"), (L2, "864035054122222"))
        self.assertEqual({e.estado for e in r.entradas}, {CONFLICTO})
        self.assertEqual(r.exactas, {})
        self.assertTrue(all("dno duplicado" in e.motivo for e in r.entradas))
        self.assertNotIn("864035054122222", " ".join(e.motivo for e in r.entradas))  # DNO enmascarado

    def test_dno_duplicado_en_dispositivos_sin_llave_se_agrupa(self):
        r = mapear([L1], (L1, "864035054122222"), ("OTRO|A", "864035054199999"), ("OTRO|B", "864035054199999"))
        conflictos = r.con_estado(CONFLICTO)
        self.assertEqual(len(conflictos), 1)
        self.assertEqual({d.name for d in conflictos[0].dispositivos}, {"OTRO|A", "OTRO|B"})
        self.assertEqual(r.resumen()["exactas"], 1)

    def test_llave_duplicada_en_el_excel(self):
        r = mapear([L1, L2, L1], (L1, "864035054122222"), (L2, "864035054133333"))
        e = self.entrada(r, L1)
        self.assertEqual(e.estado, CONFLICTO)
        self.assertIn("repetida", e.motivo)
        self.assertEqual(self.entrada(r, L2).estado, COINCIDENCIA_EXACTA)
        self.assertEqual(r.resumen()["locales"], 2)  # LLAVEs únicas

    def test_name_vacio_y_dno_vacio_son_conflictos(self):
        r = mapear([L1, L2], (L1, None), (None, "864035054133333"), (L2, "x y"))
        self.assertEqual(self.entrada(r, L1).estado, CONFLICTO)  # name presente pero sin dno
        self.assertIn("dno vacío", self.entrada(r, L1).motivo)
        self.assertEqual(self.entrada(r, L2).estado, CONFLICTO)  # dno inválido
        self.assertIn("dno inválido", self.entrada(r, L2).motivo)
        sin_name = [e for e in r.con_estado(CONFLICTO) if e.llave is None]
        self.assertEqual(len(sin_name), 1)
        self.assertIn("name vacío", sin_name[0].motivo)
        self.assertEqual(r.exactas, {})

    def test_resumen_calculado_con_los_datos(self):
        llaves = [L1, L2, L3, "AVEO/2026/BLANCO/NJG463C", "SOLO|LOCAL|X|1"]
        r = mapear(llaves, (L1, "864035054122222"), (L2, "864035054133333"), (L3, "864035054144444"),
                   ("AVEO|2026|BLANCO|NJG463C", "864035054155555", "NJG463C"), ("EXTRA|B|A|9", "864035054177777"))
        self.assertEqual(r.resumen(), {"locales": 5, "dispositivos": 5, "exactas": 3, "posibles": 1,
                                       "solo_local": 1, "solo_baanool": 1, "conflictos": 0})
        self.assertEqual(len(r.excepciones), 3)

    def test_cada_llave_y_cada_dispositivo_aparecen_una_sola_vez(self):
        llaves = [L1, L2, L3, "AVEO/2022/AZUL/PCH9635"]
        r = mapear(llaves, (L1, "864035054122222", "PCH9635"), (L2, "864035054133333"), (L3, "864035054133333"))
        etiquetas_llaves = [e.llave for e in r.entradas if e.llave]
        self.assertEqual(sorted(etiquetas_llaves), sorted(set(llaves)))
        # El dispositivo L1 coincide exactamente con L1 y además es candidato de la variante con "/".
        variante = self.entrada(r, "AVEO/2022/AZUL/PCH9635")
        self.assertEqual(variante.estado, POSIBLE_COINCIDENCIA)
        self.assertEqual(self.entrada(r, L1).estado, COINCIDENCIA_EXACTA)


if __name__ == "__main__":
    unittest.main()
