"""Pruebas del manifiesto local de adquisición (etapa 6C). HTTP simulado: NO hay llamadas reales.

    py -m unittest tests.test_manifiesto -v
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import requests

import descargar_flota_baanool as script
import src.descarga_lote as modulo_lote
from crear_carpetas_llaves import limpiar_nombre_carpeta
from src import manifiesto as m
from src.descarga_lote import ejecutar_lote
from src.manifiesto import (ErrorEscrituraManifiesto, ManifiestoInvalido, NO_PROCESADO, actualizar_manifiesto,
                            cargar_manifiesto_adquisicion, construir_manifiesto, estados_por_llave,
                            nombre_manifiesto, ruta_manifiesto)
from tests.test_descarga_lote import (FIN, GPX_3, GPX_VACIO, INICIO, NOMBRE, TOKEN, Resp, SesionLote, dno, llave,
                                      mapeo_con)

CDMX = ZoneInfo("America/Mexico_City")


def salida_falsa():
    """Un flujo de texto que acepta reconfigure() (el script lo usa) y descarta lo escrito."""
    return io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
AHORA = datetime(2026, 10, 11, 12, 0, 0, tzinfo=CDMX)
AHORA_2 = datetime(2026, 10, 11, 18, 30, 0, tzinfo=CDMX)


class BaseManifiesto(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.entrada = self.raiz / "entrada"
        self.entrada.mkdir()
        self.manifiestos = self.raiz / "salida" / "estado_descargas"

    def tearDown(self):
        self._tmp.cleanup()

    def correr(self, mapeo, sesion=None, ahora=AHORA, **kwargs):
        sesion = sesion or SesionLote()
        lote = ejecutar_lote(mapeo, INICIO, FIN, self.entrada, token=TOKEN, sesion=sesion,
                             pausar=lambda s: None, **kwargs)
        ruta = actualizar_manifiesto(lote, mapeo, self.raiz, self.manifiestos, ahora)
        return lote, ruta, json.loads(ruta.read_text(encoding="utf-8"))

    @staticmethod
    def entradas(manifiesto):
        return {v["llave"]: v for v in manifiesto["vehiculos"]}


class PruebasContenido(BaseManifiesto):
    def test_nombre_y_formato_general_con_fechas_locales(self):
        self.assertEqual(nombre_manifiesto(INICIO, FIN), "2026-10-10_000000__2026-10-10_235959.json")
        _, ruta, mf = self.correr(mapeo_con(1))
        self.assertEqual(ruta, self.manifiestos / "2026-10-10_000000__2026-10-10_235959.json")
        self.assertEqual(mf["version_formato"], 1)
        self.assertEqual(mf["periodo_inicio_local"], "2026-10-10 00:00:00")
        self.assertEqual(mf["periodo_fin_local"], "2026-10-10 23:59:59")
        self.assertEqual(mf["zona_horaria_local"], "America/Mexico_City")
        self.assertEqual(mf["fecha_ejecucion"], "2026-10-11T12:00:00-06:00")
        self.assertIn("resumen", mf)

    def test_A_descargado_se_guarda(self):
        _, _, mf = self.correr(mapeo_con(1))
        v = self.entradas(mf)[llave(0)]
        self.assertEqual(v["estado_adquisicion"], "DESCARGADO")
        self.assertEqual(v["cantidad_puntos"], 3)
        self.assertEqual(v["archivo_gpx"], f"entrada/{limpiar_nombre_carpeta(llave(0))}/{NOMBRE}")
        self.assertEqual(v["ultima_actualizacion"], "2026-10-11T12:00:00-06:00")

    def test_B_ya_existe_se_guarda(self):
        carpeta = self.entrada / limpiar_nombre_carpeta(llave(0))
        carpeta.mkdir()
        (carpeta / NOMBRE).write_bytes(b"previo")
        _, _, mf = self.correr(mapeo_con(1))
        v = self.entradas(mf)[llave(0)]
        self.assertEqual(v["estado_adquisicion"], "YA_EXISTE")
        self.assertEqual(v["archivo_gpx"], f"entrada/{limpiar_nombre_carpeta(llave(0))}/{NOMBRE}")

    def test_C_sin_datos_periodo_se_guarda_con_archivo_null(self):
        sesion = SesionLote(archivos={dno(0): GPX_VACIO})
        _, _, mf = self.correr(mapeo_con(1), sesion)
        v = self.entradas(mf)[llave(0)]
        self.assertEqual(v["estado_adquisicion"], "SIN_DATOS_PERIODO")
        self.assertIsNone(v["archivo_gpx"])
        self.assertIsNone(v["cantidad_puntos"])
        self.assertIn("no entregó posiciones", v["mensaje"])

    def test_D_solo_local_se_guarda_aunque_no_entre_al_lote(self):
        sesion = SesionLote()
        _, _, mf = self.correr(mapeo_con(1, locales_extra=["SOLO|LOCAL|X|1"]), sesion)
        v = self.entradas(mf)["SOLO|LOCAL|X|1"]
        self.assertEqual(v["estado_adquisicion"], "SOLO_LOCAL")
        self.assertIsNone(v["archivo_gpx"])
        self.assertIsNone(v["estado_actual_baanool"])  # no tiene dispositivo
        self.assertEqual(sesion.dnos_exportados, [dno(0)])  # nunca se pidió

    def test_solo_local_y_sin_datos_no_son_equivalentes(self):
        sesion = SesionLote(archivos={dno(0): GPX_VACIO})
        _, _, mf = self.correr(mapeo_con(1, locales_extra=["SOLO|LOCAL|X|1"]), sesion)
        e = self.entradas(mf)
        self.assertEqual(e[llave(0)]["estado_adquisicion"], "SIN_DATOS_PERIODO")  # con dispositivo
        self.assertEqual(e["SOLO|LOCAL|X|1"]["estado_adquisicion"], "SOLO_LOCAL")  # sin dispositivo
        self.assertEqual(mf["resumen"]["por_estado"], {"SIN_DATOS_PERIODO": 1, "SOLO_LOCAL": 1})

    def test_E_error_exportacion_se_guarda(self):
        sesion = SesionLote(exportaciones={dno(0): Resp(200, {"status": 1, "resmsg": "fallo", "data": None})})
        _, _, mf = self.correr(mapeo_con(1), sesion)
        v = self.entradas(mf)[llave(0)]
        self.assertEqual(v["estado_adquisicion"], "ERROR_EXPORTACION")
        self.assertIsNone(v["archivo_gpx"])
        self.assertIn("STATUS_BAANOOL", v["mensaje"])

    def test_otros_estados_reales_se_conservan(self):
        mapeo = mapeo_con(3, override={0: {"lat": None, "lng": None}})
        sesion = SesionLote(archivos={dno(1): Resp(404)})
        _, _, mf = self.correr(mapeo, sesion)
        e = self.entradas(mf)
        self.assertEqual(e[llave(0)]["estado_adquisicion"], "SIN_COORDENADAS")
        self.assertEqual(e[llave(1)]["estado_adquisicion"], "ERROR_DESCARGA")
        self.assertEqual(e[llave(2)]["estado_adquisicion"], "DESCARGADO")

    def test_posibles_y_conflictos_de_mapeo_quedan_registrados(self):
        mapeo = mapeo_con(1, locales_extra=["AVEO/2026/BLANCO/NJG463C", "DUP|X|Y|1"],
                          dispositivos_extra=[("AVEO|2026|BLANCO|NJG463C", dno(80), 19.4, -99.1, "NJG463C"),
                                              ("DUP|X|Y|1", dno(81)), ("DUP|X|Y|1", dno(82))])
        _, _, mf = self.correr(mapeo)
        e = self.entradas(mf)
        self.assertEqual(e["AVEO/2026/BLANCO/NJG463C"]["estado_adquisicion"], "POSIBLE_COINCIDENCIA")
        self.assertEqual(e["DUP|X|Y|1"]["estado_adquisicion"], "CONFLICTO")
        self.assertEqual(mf["resumen"]["llaves_locales"], 3)

    def test_F_online_offline_se_conserva_pero_no_altera_el_estado(self):
        mapeo = mapeo_con(3, override={0: {"online": True}, 1: {"online": False}, 2: {"online": None}})
        sesion = SesionLote(archivos={dno(0): GPX_VACIO})  # ONLINE ahora, sin datos en el periodo
        _, _, mf = self.correr(mapeo, sesion)
        e = self.entradas(mf)
        self.assertEqual((e[llave(0)]["estado_adquisicion"], e[llave(0)]["estado_actual_baanool"]),
                         ("SIN_DATOS_PERIODO", "ONLINE"))
        self.assertEqual((e[llave(1)]["estado_adquisicion"], e[llave(1)]["estado_actual_baanool"]),
                         ("DESCARGADO", "OFFLINE"))  # OFFLINE ahora, con datos del periodo
        self.assertEqual((e[llave(2)]["estado_adquisicion"], e[llave(2)]["estado_actual_baanool"]),
                         ("DESCARGADO", None))
        self.assertIn("no describe el periodo", mf["nota"])

    def test_vehiculos_ordenados_por_llave(self):
        _, _, mf = self.correr(mapeo_con(5, locales_extra=["AAA|SOLO|X|1"]))
        nombres = [v["llave"] for v in mf["vehiculos"]]
        self.assertEqual(nombres, sorted(nombres))


class PruebasSeguridad(BaseManifiesto):
    def test_G_las_rutas_son_relativas_y_portables(self):
        sesion = SesionLote(archivos={dno(1): b"<?xml version='1.0'?><gpx><trk><trkpt"})  # GPX ilegible
        _, ruta, mf = self.correr(mapeo_con(3), sesion)
        for v in mf["vehiculos"]:
            if v["archivo_gpx"]:
                self.assertTrue(v["archivo_gpx"].startswith("entrada/"))
                self.assertNotIn("\\", v["archivo_gpx"])
                self.assertNotRegex(v["archivo_gpx"], r"^[A-Za-z]:")
        texto = ruta.read_text(encoding="utf-8")
        self.assertNotIn(str(self.raiz), texto)  # ninguna ruta absoluta del equipo
        self.assertNotIn(str(self.raiz.resolve()), texto)
        self.assertNotRegex(texto, r"[A-Za-z]:\\\\")  # ni 'C:\\...' escapado en JSON
        invalido = self.entradas(mf)[llave(1)]
        self.assertEqual(invalido["estado_adquisicion"], "GPX_INVALIDO")

    def test_H_el_token_nunca_aparece_en_el_json(self):
        sesion = SesionLote(
            exportaciones={dno(0): Resp(200, {"status": 1, "resmsg": f"error {TOKEN}", "data": None}),
                           dno(1): requests.exceptions.ConnectionError(f"fallo {TOKEN}")},
            archivos={dno(2): Resp(200, contenido=b"<html>" + TOKEN.encode() + b"</html>")})
        _, ruta, _ = self.correr(mapeo_con(3), sesion)
        texto = ruta.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, texto)
        self.assertNotIn("https://", texto)  # ni URLs de descarga
        for clave in ('"token"', "cookie", "Authorization", "dno"):
            self.assertNotIn(clave.lower(), texto.lower())

    def test_interrupcion_por_token_tampoco_filtra_el_token(self):
        sesion = SesionLote(exportaciones={dno(1): Resp(401)})
        _, ruta, mf = self.correr(mapeo_con(3), sesion)
        self.assertFalse(mf["lote_completo"])
        self.assertNotIn(TOKEN, ruta.read_text(encoding="utf-8"))


class PruebasActualizacionYCompletitud(BaseManifiesto):
    def test_J_segunda_ejecucion_actualiza_error_a_descargado(self):
        falla = SesionLote(exportaciones={dno(1): Resp(200, {"status": 1, "resmsg": "x", "data": None})})
        _, _, mf1 = self.correr(mapeo_con(3), falla, ahora=AHORA)
        self.assertEqual(self.entradas(mf1)[llave(1)]["estado_adquisicion"], "ERROR_EXPORTACION")

        _, _, mf2 = self.correr(mapeo_con(3), SesionLote(), ahora=AHORA_2)
        e = self.entradas(mf2)
        self.assertEqual(e[llave(1)]["estado_adquisicion"], "DESCARGADO")  # ya no contradictorio
        self.assertEqual(e[llave(1)]["ultima_actualizacion"], "2026-10-11T18:30:00-06:00")
        self.assertEqual(len(mf2["vehiculos"]), 3)  # una entrada por LLAVE: no se acumula
        self.assertEqual(e[llave(0)]["estado_adquisicion"], "YA_EXISTE")  # la primera ya estaba descargada
        self.assertIsNone(e[llave(1)]["mensaje"] if "STATUS" in (e[llave(1)]["mensaje"] or "") else None)

    def test_K_sin_datos_en_el_manifiesto_no_bloquea_una_consulta_futura(self):
        self.correr(mapeo_con(1), SesionLote(archivos={dno(0): GPX_VACIO}))
        mf = cargar_manifiesto_adquisicion(INICIO, FIN, self.manifiestos)
        self.assertEqual(estados_por_llave(mf)[llave(0)]["estado_adquisicion"], "SIN_DATOS_PERIODO")
        # Nueva corrida: el dispositivo ya reporta -> se vuelve a pedir a BAANOOL y se descarga.
        sesion = SesionLote()
        _, _, mf2 = self.correr(mapeo_con(1), sesion)
        self.assertEqual(len(sesion.llamadas_exportacion), 1)
        self.assertEqual(self.entradas(mf2)[llave(0)]["estado_adquisicion"], "DESCARGADO")
        # La lógica de descarga no lee el manifiesto en absoluto.
        self.assertNotIn("manifiesto", Path(modulo_lote.__file__).read_text(encoding="utf-8").lower())

    def test_L_lote_completo_es_true(self):
        _, _, mf = self.correr(mapeo_con(3))
        self.assertTrue(mf["lote_completo"])
        self.assertIsNone(mf["motivo_interrupcion"])

    def test_M_interrupcion_por_rate_limit_es_false_con_motivo(self):
        sesion = SesionLote(exportaciones={dno(2): Resp(200, {"status": 5550, "resmsg": "Too many requests"})})
        lote, _, mf = self.correr(mapeo_con(5), sesion)
        self.assertFalse(mf["lote_completo"])
        self.assertIn("RATE_LIMIT_BAANOOL", mf["motivo_interrupcion"])
        e = self.entradas(mf)
        self.assertEqual([e[llave(i)]["estado_adquisicion"] for i in range(5)],
                         ["DESCARGADO", "DESCARGADO", "ERROR_EXPORTACION", NO_PROCESADO, NO_PROCESADO])
        self.assertIn("RATE_LIMIT_BAANOOL", e[llave(3)]["mensaje"])

    def test_M2_limite_marca_lote_incompleto(self):
        _, _, mf = self.correr(mapeo_con(5), limite=2)
        self.assertFalse(mf["lote_completo"])
        self.assertIn("--limite", mf["motivo_interrupcion"])
        self.assertEqual(self.entradas(mf)[llave(4)]["estado_adquisicion"], NO_PROCESADO)

    def test_corrida_parcial_conserva_lo_ya_conocido_de_los_no_procesados(self):
        self.correr(mapeo_con(4), SesionLote(archivos={dno(3): GPX_VACIO}), ahora=AHORA)
        _, _, mf = self.correr(mapeo_con(4), SesionLote(), ahora=AHORA_2, limite=2)
        e = self.entradas(mf)
        self.assertFalse(mf["lote_completo"])
        self.assertEqual(e[llave(0)]["ultima_actualizacion"], "2026-10-11T18:30:00-06:00")  # procesada ahora
        self.assertEqual(e[llave(2)]["estado_adquisicion"], "DESCARGADO")  # no procesada: se conserva
        self.assertEqual(e[llave(2)]["ultima_actualizacion"], "2026-10-11T12:00:00-06:00")
        self.assertEqual(e[llave(3)]["estado_adquisicion"], "SIN_DATOS_PERIODO")  # no se degrada a NO_PROCESADO

    def test_dry_run_no_genera_manifiesto_con_la_funcion(self):
        mapeo = mapeo_con(2)
        lote = ejecutar_lote(mapeo, INICIO, FIN, self.entrada, token=None, dry_run=True)
        with self.assertRaises(ValueError):
            construir_manifiesto(lote, mapeo, self.raiz, AHORA)


class PruebasScript(BaseManifiesto):
    def correr_script(self, *args, sesion=None):
        mapeo = mapeo_con(3, locales_extra=["SOLO|LOCAL|X|1"])
        argv = ["descargar_flota_baanool.py", "--inicio", "2026-10-10 00:00:00", "--fin", "2026-10-10 23:59:59",
                "--entrada", str(self.entrada), "--manifiestos", str(self.manifiestos), *args]
        sesion = sesion or SesionLote()
        lote_real = modulo_lote.ejecutar_lote
        sin_pausa = lambda *a, **k: lote_real(*a, pausar=lambda s: None, **k)  # no esperar en las pruebas
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(script, "obtener_token", lambda: TOKEN), \
                mock.patch.object(script, "preparar_mapeo", lambda token, excel: (mapeo, 0)), \
                mock.patch.object(script, "ejecutar_lote", sin_pausa), \
                mock.patch.object(modulo_lote, "crear_sesion", lambda: sesion), \
                contextlib.redirect_stdout(salida_falsa()), contextlib.redirect_stderr(salida_falsa()):
            return script.main(), sesion

    def test_I_dry_run_no_escribe_manifiesto(self):
        codigo, sesion = self.correr_script("--dry-run")
        self.assertEqual(codigo, 0)
        self.assertEqual(sesion.llamadas_exportacion, [])
        self.assertFalse(self.manifiestos.exists())

    def test_la_ejecucion_real_escribe_el_manifiesto(self):
        codigo, _ = self.correr_script()
        self.assertEqual(codigo, 0)
        mf = cargar_manifiesto_adquisicion(INICIO, FIN, self.manifiestos)
        self.assertEqual(mf["resumen"]["por_estado"], {"DESCARGADO": 3, "SOLO_LOCAL": 1})
        self.assertTrue(mf["lote_completo"])

    def test_el_manifiesto_no_cambia_el_codigo_de_salida_por_sin_datos(self):
        codigo, _ = self.correr_script(sesion=SesionLote(archivos={dno(0): GPX_VACIO}))
        self.assertEqual(codigo, 0)  # SIN_DATOS_PERIODO no es un error


class PruebasEscrituraYLectura(BaseManifiesto):
    def test_N_escritura_atomica_no_destruye_un_manifiesto_valido(self):
        destino = ruta_manifiesto(INICIO, FIN, self.manifiestos)
        _, _, _ = self.correr(mapeo_con(2))
        antes = destino.read_bytes()

        def dump_roto(obj, archivo, **kwargs):
            archivo.write('{"version_formato": 1, "vehiculos": [')  # JSON incompleto
            raise RuntimeError("fallo a mitad de la escritura")

        with mock.patch.object(m.json, "dump", dump_roto):
            with self.assertRaises(ErrorEscrituraManifiesto):
                self.correr(mapeo_con(2), SesionLote(), ahora=AHORA_2)
        self.assertEqual(destino.read_bytes(), antes)  # el válido sigue intacto
        self.assertEqual(sorted(p.name for p in destino.parent.iterdir()), [destino.name])  # sin .tmp
        json.loads(destino.read_text(encoding="utf-8"))

    def test_N2_un_fallo_sin_manifiesto_previo_no_deja_nada(self):
        with mock.patch.object(m.json, "dump", side_effect=RuntimeError("x")):
            with self.assertRaises(ErrorEscrituraManifiesto):
                self.correr(mapeo_con(1))
        self.assertEqual([p for p in self.manifiestos.rglob("*") if p.is_file()], [])

    def test_O_la_lectura_recupera_el_manifiesto(self):
        _, ruta, escrito = self.correr(mapeo_con(2, locales_extra=["SOLO|LOCAL|X|1"]))
        leido = cargar_manifiesto_adquisicion(INICIO, FIN, self.manifiestos)
        self.assertEqual(leido, escrito)
        estados = estados_por_llave(leido)
        self.assertEqual(set(estados), {llave(0), llave(1), "SOLO|LOCAL|X|1"})
        self.assertEqual(estados[llave(0)]["estado_adquisicion"], "DESCARGADO")

    def test_P_manifiesto_inexistente_devuelve_none(self):
        self.assertIsNone(cargar_manifiesto_adquisicion(INICIO, FIN, self.manifiestos))
        otro = (datetime(2030, 1, 1), datetime(2030, 1, 2))
        self.correr(mapeo_con(1))
        self.assertIsNone(cargar_manifiesto_adquisicion(*otro, self.manifiestos))

    def test_P2_manifiesto_ilegible_o_de_otra_version_es_explicito(self):
        ruta = ruta_manifiesto(INICIO, FIN, self.manifiestos)
        ruta.parent.mkdir(parents=True)
        ruta.write_text("{ no es json", encoding="utf-8")
        with self.assertRaises(ManifiestoInvalido):
            cargar_manifiesto_adquisicion(INICIO, FIN, self.manifiestos)
        ruta.write_text(json.dumps({"version_formato": 99, "vehiculos": []}), encoding="utf-8")
        with self.assertRaises(ManifiestoInvalido):
            cargar_manifiesto_adquisicion(INICIO, FIN, self.manifiestos)

    def test_un_manifiesto_ilegible_se_reemplaza_en_la_siguiente_corrida(self):
        ruta = ruta_manifiesto(INICIO, FIN, self.manifiestos)
        ruta.parent.mkdir(parents=True)
        ruta.write_text("{ corrupto", encoding="utf-8")
        _, _, mf = self.correr(mapeo_con(1))
        self.assertEqual(self.entradas(mf)[llave(0)]["estado_adquisicion"], "DESCARGADO")


if __name__ == "__main__":
    unittest.main()
