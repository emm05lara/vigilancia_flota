"""Pruebas de la integración BAANOOL -> MANIFIESTO -> ANÁLISIS (etapa 7). Sin red y sin Streamlit.

    py -m unittest tests.test_integracion_periodo -v
"""

import csv
import io
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import requests

import src.integracion_periodo as ip
from crear_carpetas_llaves import limpiar_nombre_carpeta
from src.analizador import analizar_carpeta
from src.descarga_lote import nombre_archivo_lote
from src.manifiesto import (ManifiestoInvalido, escribir_manifiesto, nombre_manifiesto, ruta_manifiesto)
from src.prioridades import (DATOS_ERROR_ADQUISICION, DATOS_OK, DATOS_SIN_DATOS_PERIODO, DATOS_SIN_DISPOSITIVO,
                             DATOS_SIN_GPX, NORMAL, evaluar_vehiculo, filtrar_evaluaciones, generar_csv,
                             resumir_flota)
from tests.test_descarga_lote import (FIN, GPX_1, GPX_3, INICIO, NOMBRE, TOKEN, Resp, SesionLote, dno, llave,
                                      mapeo_con)
from tests.test_mapeo_dno import crear_excel
from tests.test_prioridades import ZONAS

OTRO_INICIO, OTRO_FIN = datetime(2026, 10, 11, 0, 0, 0), datetime(2026, 10, 11, 23, 59, 59)
NOMBRE_OTRO = nombre_archivo_lote(OTRO_INICIO, OTRO_FIN)


def entrada_manifiesto(llave_, estado, archivo=None, actual=None, puntos=None, mensaje=None):
    return {"llave": llave_, "estado_adquisicion": estado, "estado_actual_baanool": actual,
            "archivo_gpx": archivo, "cantidad_puntos": puntos, "mensaje": mensaje,
            "ultima_actualizacion": "2026-10-11T12:00:00-06:00"}


def manifiesto_de(entradas, inicio=INICIO, fin=FIN):
    return {"version_formato": 1, "periodo_inicio_local": inicio.strftime("%Y-%m-%d %H:%M:%S"),
            "periodo_fin_local": fin.strftime("%Y-%m-%d %H:%M:%S"), "zona_horaria_local": "America/Mexico_City",
            "fecha_ejecucion": "2026-10-11T12:00:00-06:00", "lote_completo": True, "motivo_interrupcion": None,
            "nota": "", "resumen": {}, "vehiculos": list(entradas)}


class BasePeriodo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.entrada = self.raiz / "entrada"
        self.entrada.mkdir()
        self.manifiestos = self.raiz / "salida" / "estado_descargas"

    def tearDown(self):
        self._tmp.cleanup()

    def carpeta(self, n):
        return self.entrada / limpiar_nombre_carpeta(llave(n))

    def poner_gpx(self, n, nombre=NOMBRE, contenido=GPX_3):
        carpeta = self.carpeta(n)
        carpeta.mkdir(exist_ok=True)
        (carpeta / nombre).write_bytes(contenido)
        return f"entrada/{carpeta.name}/{nombre}"

    def evaluar(self, entradas, llaves=None, zonas=ZONAS):
        manifiesto = manifiesto_de(entradas)
        llaves = llaves or [e["llave"] for e in entradas]
        contextos = ip.construir_contextos(llaves, manifiesto, self.raiz)
        return contextos, {e.identificador: e for e in ip.evaluar_contextos(contextos, self.raiz, zonas)}


class PruebasClasificacion(BasePeriodo):
    def test_A_descargado_con_archivo_existente_es_analizable(self):
        ruta = self.poner_gpx(0)
        contextos, ev = self.evaluar([entrada_manifiesto(llave(0), "DESCARGADO", ruta, "ONLINE", 3)])
        self.assertTrue(contextos[0].analizable)
        e = ev[llave(0)]
        self.assertEqual(e.estado_datos, DATOS_OK)
        self.assertEqual(e.estado_adquisicion, "DESCARGADO")
        self.assertGreater(e.distancia_observada_km, 0)

    def test_B_ya_existe_con_archivo_existente_es_analizable(self):
        ruta = self.poner_gpx(0)
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "YA_EXISTE", ruta)])
        self.assertEqual(ev[llave(0)].estado_datos, DATOS_OK)

    def test_C_sin_datos_periodo_no_analiza_gpx_ni_tiene_prioridad(self):
        self.poner_gpx(0, "otro_manual.gpx")  # un GPX cualquiera en la carpeta: no debe usarse
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "SIN_DATOS_PERIODO", actual="ONLINE")])
        e = ev[llave(0)]
        self.assertEqual(e.estado_datos, DATOS_SIN_DATOS_PERIODO)
        self.assertIsNone(e.prioridad)
        self.assertEqual(e.resultado.gpx, [])  # no se leyó ningún archivo
        self.assertEqual(e.resultado.puntos, [])
        self.assertIn("BAANOOL no entregó posiciones GPS para el periodo seleccionado", " ".join(e.motivos))

    def test_D_solo_local_es_sin_dispositivo_sin_prioridad(self):
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "SOLO_LOCAL")])
        e = ev[llave(0)]
        self.assertEqual(e.estado_datos, DATOS_SIN_DISPOSITIVO)
        self.assertIsNone(e.prioridad)
        self.assertIn("no tiene un dispositivo BAANOOL asociado", " ".join(e.motivos))
        self.assertIsNone(e.estado_actual_baanool)

    def test_E_errores_de_adquisicion(self):
        entradas = [entrada_manifiesto(llave(i), estado, mensaje="detalle")
                    for i, estado in enumerate(["ERROR_EXPORTACION", "ERROR_DESCARGA", "SIN_COORDENADAS",
                                                "CARPETA_AMBIGUA"])]
        _, ev = self.evaluar(entradas)
        for i in range(4):
            self.assertEqual(ev[llave(i)].estado_datos, DATOS_ERROR_ADQUISICION)
            self.assertIsNone(ev[llave(i)].prioridad)

    def test_F_gpx_invalido_de_adquisicion_es_error_de_adquisicion(self):
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "GPX_INVALIDO", mensaje="XML corrupto")])
        self.assertEqual(ev[llave(0)].estado_datos, DATOS_ERROR_ADQUISICION)

    def test_G_manifiesto_dice_ya_existe_pero_el_archivo_falta(self):
        self.poner_gpx(0, "F86AYT.gpx")  # otro GPX en la carpeta: NO se busca silenciosamente
        ruta = f"entrada/{self.carpeta(0).name}/{NOMBRE}"  # el referenciado no existe
        contextos, ev = self.evaluar([entrada_manifiesto(llave(0), "YA_EXISTE", ruta)])
        e = ev[llave(0)]
        self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
        self.assertIsNone(e.prioridad)
        self.assertEqual(e.resultado.gpx, [])
        self.assertIn("ya no existe", e.inconsistencia)
        self.assertEqual(e.estado_adquisicion, "YA_EXISTE")

    def test_H_gpx_de_otro_periodo_en_la_misma_carpeta_no_se_analiza(self):
        self.poner_gpx(0, NOMBRE_OTRO)  # BAANOOL de OTRO periodo; el del periodo seleccionado no existe
        ruta = f"entrada/{self.carpeta(0).name}/{NOMBRE}"
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "DESCARGADO", ruta)])
        self.assertEqual(ev[llave(0)].estado_datos, DATOS_SIN_GPX)
        self.assertEqual(ev[llave(0)].resultado.gpx, [])

    def test_H2_manifiesto_que_apunta_a_un_archivo_de_otro_periodo_se_rechaza(self):
        ruta_otro = self.poner_gpx(0, NOMBRE_OTRO)  # el archivo existe pero es de otro periodo
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "DESCARGADO", ruta_otro)])
        e = ev[llave(0)]
        self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
        self.assertIn("no corresponde al periodo", e.inconsistencia)

    def test_I_solo_se_analiza_el_archivo_indicado_por_el_manifiesto(self):
        ruta = self.poner_gpx(0, NOMBRE, GPX_3)
        self.poner_gpx(0, NOMBRE_OTRO, GPX_1)
        self.poner_gpx(0, "F86AYT.gpx", GPX_1)
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "YA_EXISTE", ruta)])
        v = ev[llave(0)].resultado
        self.assertEqual([g.archivo.name for g in v.gpx], [NOMBRE])
        self.assertEqual(len(v.puntos), 3)  # no se sumaron los puntos de los otros dos archivos

    def test_AC_el_modo_baanool_no_mezcla_gpx_manuales(self):
        ruta = self.poner_gpx(0, NOMBRE, GPX_3)
        self.poner_gpx(0, "manual_historico.gpx", GPX_1)
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "YA_EXISTE", ruta)])
        self.assertEqual(len(ev[llave(0)].resultado.puntos), 3)
        # El modo local sí los consolida, como siempre (3 + 1 puntos distintos).
        local = analizar_carpeta(self.carpeta(0), {}, ZONAS)
        self.assertEqual(len(local.gpx), 2)

    def test_K_online_no_cambia_sin_datos_periodo(self):
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "SIN_DATOS_PERIODO", actual="ONLINE")])
        self.assertEqual(ev[llave(0)].estado_datos, DATOS_SIN_DATOS_PERIODO)
        self.assertEqual(ev[llave(0)].estado_actual_baanool, "ONLINE")  # se conserva solo como dato

    def test_L_offline_no_invalida_un_gpx_historico_valido(self):
        ruta = self.poner_gpx(0)
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "DESCARGADO", ruta, actual="OFFLINE")])
        e = ev[llave(0)]
        self.assertEqual(e.estado_datos, DATOS_OK)
        self.assertEqual(e.estado_actual_baanool, "OFFLINE")
        self.assertIsNotNone(e.prioridad)

    def test_M_vehiculo_valido_mantiene_la_prioridad_calculada_existente(self):
        ruta = self.poner_gpx(0)
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "DESCARGADO", ruta)])
        esperado = evaluar_vehiculo(analizar_carpeta(self.carpeta(0), {llave(0).casefold(): llave(0)}, ZONAS))
        e = ev[llave(0)]
        self.assertEqual(e.prioridad, esperado.prioridad)
        self.assertAlmostEqual(e.distancia_observada_km, esperado.distancia_observada_km)
        self.assertEqual(e.nivel_movimiento, esperado.nivel_movimiento)
        self.assertEqual(e.calidad_gps, esperado.calidad_gps)
        self.assertEqual(e.trayectos, esperado.trayectos)

    def test_N_sin_datos_nunca_recibe_normal(self):
        entradas = [entrada_manifiesto(llave(0), "SIN_DATOS_PERIODO"), entrada_manifiesto(llave(1), "SOLO_LOCAL"),
                    entrada_manifiesto(llave(2), "ERROR_EXPORTACION"),
                    entrada_manifiesto(llave(3), "YA_EXISTE", "entrada/x/falta.gpx")]
        _, ev = self.evaluar(entradas)
        for e in ev.values():
            self.assertIsNone(e.prioridad)
            self.assertNotEqual(e.prioridad, NORMAL)
            self.assertFalse(e.requiere_atencion)
            self.assertTrue(e.problema_de_datos)
            self.assertIsNone(e.distancia_observada_km)  # y no 0 km

    def test_O_calidad_gps_no_se_evalua_sin_puntos(self):
        entradas = [entrada_manifiesto(llave(0), "SIN_DATOS_PERIODO"), entrada_manifiesto(llave(1), "SOLO_LOCAL")]
        _, ev = self.evaluar(entradas)
        for e in ev.values():
            self.assertIsNone(e.calidad_gps)  # ni SIN_ALERTAS ni REVISAR
            self.assertIsNone(e.resultado.calidad_gps)

    def test_P_Q_sin_datos_no_generan_mapa_y_con_datos_si(self):
        ruta = self.poner_gpx(0)
        entradas = [entrada_manifiesto(llave(0), "DESCARGADO", ruta), entrada_manifiesto(llave(1), "SIN_DATOS_PERIODO"),
                    entrada_manifiesto(llave(2), "SOLO_LOCAL"), entrada_manifiesto(llave(3), "ERROR_DESCARGA"),
                    entrada_manifiesto(llave(4), "YA_EXISTE", "entrada/x/falta.gpx")]
        _, ev = self.evaluar(entradas)
        self.assertTrue(ip.puede_generar_mapa(ev[llave(0)]))
        for n in (1, 2, 3, 4):
            self.assertFalse(ip.puede_generar_mapa(ev[llave(n)]))

    def test_estado_no_reconocido_y_no_procesado(self):
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "NO_PROCESADO", mensaje="--limite"),
                              entrada_manifiesto(llave(1), "ESTADO_NUEVO_RARO")])
        self.assertEqual(ev[llave(0)].estado_datos, DATOS_SIN_GPX)
        self.assertEqual(ev[llave(1)].estado_datos, DATOS_ERROR_ADQUISICION)

    def test_posible_coincidencia_y_conflicto_no_tienen_dispositivo_exacto(self):
        _, ev = self.evaluar([entrada_manifiesto(llave(0), "POSIBLE_COINCIDENCIA", mensaje="separador"),
                              entrada_manifiesto(llave(1), "CONFLICTO", mensaje="duplicado")])
        self.assertEqual({e.estado_datos for e in ev.values()}, {DATOS_SIN_DISPOSITIVO})


class PruebasCatalogoYRutas(BasePeriodo):
    def test_J_92_llaves_del_catalogo_producen_92_registros(self):
        llaves = [f"VEHICULO|{n:03d}|X|Y" for n in range(92)]
        # Solo 10 tienen GPX; 5 sin datos; 3 error; el resto solo local.
        entradas = []
        for n, l in enumerate(llaves):
            carpeta = self.entrada / limpiar_nombre_carpeta(l)
            if n < 10:
                carpeta.mkdir()
                (carpeta / NOMBRE).write_bytes(GPX_3)
                entradas.append(entrada_manifiesto(l, "YA_EXISTE", f"entrada/{carpeta.name}/{NOMBRE}", "ONLINE"))
            elif n < 15:
                entradas.append(entrada_manifiesto(l, "SIN_DATOS_PERIODO", actual="OFFLINE"))
            elif n < 18:
                entradas.append(entrada_manifiesto(l, "ERROR_EXPORTACION"))
            else:
                entradas.append(entrada_manifiesto(l, "SOLO_LOCAL"))
        contextos = ip.construir_contextos(llaves, manifiesto_de(entradas), self.raiz)
        evaluaciones = ip.evaluar_contextos(contextos, self.raiz, ZONAS)
        self.assertEqual(len(contextos), 92)
        self.assertEqual(len(evaluaciones), 92)
        r = resumir_flota(evaluaciones)
        self.assertEqual((r.analizados, r.sin_datos_periodo, r.errores_adquisicion, r.sin_dispositivo),
                         (10, 5, 3, 74))
        self.assertEqual(r.carpetas_encontradas, 92)

    def test_llave_del_catalogo_ausente_del_manifiesto_es_visible(self):
        contextos = ip.construir_contextos([llave(0), llave(1)],
                                           manifiesto_de([entrada_manifiesto(llave(0), "SOLO_LOCAL")]), self.raiz)
        faltante = [c for c in contextos if c.llave == llave(1)][0]
        self.assertEqual(faltante.estado_datos, DATOS_SIN_GPX)
        self.assertIsNone(faltante.estado_adquisicion)  # no se inventa un estado de adquisición

    def test_AA_rutas_relativas_del_manifiesto_se_resuelven(self):
        ruta, problema = ip.resolver_ruta_gpx(f"entrada/ALGO - X/{NOMBRE}", self.raiz)
        self.assertIsNone(problema)
        self.assertEqual(ruta, (self.raiz / "entrada" / "ALGO - X" / NOMBRE).resolve())

    def test_AB_rutas_inseguras_no_se_aceptan(self):
        fuera = self.raiz.parent / "secreto.gpx"
        inseguras = ["../secreto.gpx", "entrada/../../secreto.gpx", "..\\secreto.gpx", "/etc/passwd.gpx",
                     "C:\\Windows\\sistema.gpx", "\\\\servidor\\recurso\\a.gpx", "entrada/datos.xlsx", "", None, 5]
        for texto in inseguras:
            ruta, problema = ip.resolver_ruta_gpx(texto, self.raiz)
            self.assertIsNone(ruta, texto)
            self.assertTrue(problema, texto)
        self.assertFalse(fuera.exists())

    def test_AB2_manifiesto_manipulado_no_hace_leer_archivos_externos(self):
        externo = self.raiz.parent / "externo_secreto.gpx"
        externo.write_bytes(GPX_3)
        try:
            _, ev = BasePeriodo.evaluar(self, [entrada_manifiesto(llave(0), "DESCARGADO",
                                                                  "../externo_secreto.gpx")])
            e = ev[llave(0)]
            self.assertEqual(e.estado_datos, DATOS_SIN_GPX)
            self.assertEqual(e.resultado.gpx, [])
            self.assertIn("Ruta insegura", e.inconsistencia)
        finally:
            externo.unlink()


class PruebasFlujoYSesion(BasePeriodo):
    def excel(self, llaves):
        return crear_excel(self.raiz, [(l, None) for l in llaves], con_dno=False)

    def test_analizar_periodo_sin_manifiesto_devuelve_none(self):
        excel = self.excel([llave(0)])
        self.assertIsNone(ip.analizar_periodo(INICIO, FIN, excel, self.raiz, ZONAS, self.manifiestos))

    def test_analizar_periodo_con_manifiesto(self):
        ruta = self.poner_gpx(0)
        excel = self.excel([llave(0), llave(1), llave(2)])
        manifiesto = manifiesto_de([entrada_manifiesto(llave(0), "YA_EXISTE", ruta, "ONLINE"),
                                    entrada_manifiesto(llave(1), "SIN_DATOS_PERIODO", actual="ONLINE"),
                                    entrada_manifiesto(llave(2), "SOLO_LOCAL"),
                                    entrada_manifiesto("FUERA|DEL|CATALOGO|1", "SOLO_LOCAL")])
        escribir_manifiesto(manifiesto, ruta_manifiesto(INICIO, FIN, self.manifiestos))
        r = ip.analizar_periodo(INICIO, FIN, excel, self.raiz, ZONAS, self.manifiestos)
        self.assertEqual(len(r.evaluaciones), 3)  # el catálogo manda: 3 LLAVEs
        self.assertEqual(r.clave_periodo, "2026-10-10_000000__2026-10-10_235959")
        self.assertEqual(r.llaves_fuera_del_catalogo, ["FUERA|DEL|CATALOGO|1"])
        self.assertTrue(r.ruta_manifiesto.endswith("2026-10-10_000000__2026-10-10_235959.json"))
        self.assertNotIn(str(self.raiz), r.ruta_manifiesto)  # ruta relativa
        self.assertTrue(r.lote_completo)

    def test_manifiesto_ilegible_se_informa_no_se_inventa(self):
        excel = self.excel([llave(0)])
        ruta = ruta_manifiesto(INICIO, FIN, self.manifiestos)
        ruta.parent.mkdir(parents=True)
        ruta.write_text("{ no es json", encoding="utf-8")
        with self.assertRaises(ManifiestoInvalido):
            ip.analizar_periodo(INICIO, FIN, excel, self.raiz, ZONAS, self.manifiestos)

    def test_AD_modo_local_conserva_su_comportamiento(self):
        self.poner_gpx(0, NOMBRE, GPX_3)
        self.poner_gpx(0, "otro.gpx", GPX_1)
        self.carpeta(1).mkdir()
        excel = self.excel([llave(0), llave(1)])
        locales = ip.analizar_flota_local(self.entrada, excel, ZONAS)
        por_llave = {e.identificador: e for e in locales}
        self.assertEqual(len(locales), 2)
        self.assertEqual(len(por_llave[llave(0)].resultado.gpx), 2)  # consolida todos los .gpx, como antes
        self.assertEqual(por_llave[llave(1)].estado_datos, DATOS_SIN_GPX)  # carpeta sin GPX
        self.assertIsNone(por_llave[llave(0)].estado_adquisicion)  # sin manifiesto: nada inventado

    def test_V_token_ausente_no_impide_el_analisis_local(self):
        self.poner_gpx(0)
        excel = self.excel([llave(0)])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAANOOL_TOKEN", None)
            self.assertFalse(ip.token_configurado())
            locales = ip.analizar_flota_local(self.entrada, excel, ZONAS)
        self.assertEqual(locales[0].estado_datos, DATOS_OK)

    def test_token_configurado_solo_comprueba_presencia(self):
        with mock.patch.dict(os.environ, {"BAANOOL_TOKEN": TOKEN}):
            self.assertTrue(ip.token_configurado())
        with mock.patch.dict(os.environ, {"BAANOOL_TOKEN": "   "}):
            self.assertFalse(ip.token_configurado())

    def test_W_un_error_de_baanool_no_elimina_los_resultados_previos(self):
        estado = {"evaluaciones": ["previa"], "resumen": "r", "csv": "c", "periodo_analizado": {"clave": "x"}}
        excel = self.excel([llave(0)])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAANOOL_TOKEN", None)
            r = ip.actualizar_desde_baanool(INICIO, FIN, self.entrada, excel, self.raiz, self.manifiestos)
        self.assertFalse(r.ok)
        self.assertEqual(r.codigo_error, "TOKEN_AUSENTE")
        ip.aplicar_resultado_adquisicion(estado, r)
        self.assertEqual(estado["evaluaciones"], ["previa"])  # el análisis anterior queda intacto
        self.assertEqual(estado["periodo_analizado"], {"clave": "x"})
        self.assertEqual(estado["adquisicion"]["codigo_error"], "TOKEN_AUSENTE")
        self.assertFalse(ruta_manifiesto(INICIO, FIN, self.manifiestos).exists())

    def test_X_Y_cambiar_filtros_no_adquiere_ni_analiza(self):
        _, ev = BasePeriodo.evaluar(self, [entrada_manifiesto(llave(0), "SIN_DATOS_PERIODO"),
                                           entrada_manifiesto(llave(1), "SOLO_LOCAL")])
        evaluaciones = list(ev.values())
        prohibido = mock.Mock(side_effect=AssertionError("no debe ejecutarse al filtrar"))
        with mock.patch.object(ip, "ejecutar_lote", prohibido), mock.patch.object(ip, "preparar_mapeo", prohibido), \
                mock.patch.object(ip, "analizar_archivos", prohibido), \
                mock.patch.object(ip, "cargar_manifiesto_adquisicion", prohibido), \
                mock.patch("src.baanool.crear_sesion", prohibido), \
                mock.patch.object(requests.Session, "request", prohibido):
            for kwargs in ({}, {"estados_datos": [DATOS_SIN_DATOS_PERIODO]}, {"problemas_datos": True},
                           {"solo_atencion": True}, {"prioridades": ["CRITICA"]}):
                filtrar_evaluaciones(evaluaciones, **kwargs)
        prohibido.assert_not_called()

    def test_filtros_de_estado_de_datos_y_problemas(self):
        ruta = self.poner_gpx(0)
        _, ev = BasePeriodo.evaluar(self, [
            entrada_manifiesto(llave(0), "DESCARGADO", ruta), entrada_manifiesto(llave(1), "SIN_DATOS_PERIODO"),
            entrada_manifiesto(llave(2), "SOLO_LOCAL"), entrada_manifiesto(llave(3), "ERROR_EXPORTACION"),
            entrada_manifiesto(llave(4), "YA_EXISTE", "entrada/x/falta.gpx")])
        todas = list(ev.values())
        nombres = lambda lista: {e.identificador for e in lista}
        self.assertEqual(nombres(filtrar_evaluaciones(todas, estados_datos=[DATOS_SIN_DATOS_PERIODO])), {llave(1)})
        self.assertEqual(nombres(filtrar_evaluaciones(todas, problemas_datos=True)),
                         {llave(1), llave(2), llave(3), llave(4)})
        self.assertNotIn(llave(1), nombres(filtrar_evaluaciones(todas, solo_atencion=True)))  # no es prioridad

    def test_filtro_de_texto_busca_en_la_llave_sin_distinguir_mayusculas(self):
        _, ev = BasePeriodo.evaluar(self, [entrada_manifiesto("AVEO|2022|AZUL|PCH9635", "SOLO_LOCAL"),
                                           entrada_manifiesto("VENTO|2020|GRIS|R66BER", "SOLO_LOCAL")])
        todas = list(ev.values())
        self.assertEqual({e.identificador for e in filtrar_evaluaciones(todas, texto="pch96")},
                         {"AVEO|2022|AZUL|PCH9635"})
        self.assertEqual(len(filtrar_evaluaciones(todas, texto="  ")), 2)  # vacío: no filtra
        self.assertEqual(filtrar_evaluaciones(todas, texto="no-existe"), [])

    def test_Z_periodo_seleccionado_distinto_del_analizado_se_detecta(self):
        estado = {"periodo_analizado": {"clave": ip.periodo_key(INICIO, FIN), "inicio": "2026-10-10 00:00:00",
                                        "fin": "2026-10-10 23:59:59", "modo": "PERIODO"}}
        self.assertIsNone(ip.advertencia_periodo(ip.periodo_key(INICIO, FIN), estado))
        texto = ip.advertencia_periodo(ip.periodo_key(OTRO_INICIO, OTRO_FIN), estado)
        self.assertIn("2026-10-10 00:00:00 -> 2026-10-10 23:59:59", texto)
        self.assertIsNone(ip.advertencia_periodo("cualquiera", {}))  # sin análisis previo: nada que advertir
        self.assertIsNone(ip.advertencia_periodo("cualquiera", {"periodo_analizado": {"clave": "x", "modo": "LOCAL"}}))

    def test_parsear_periodo(self):
        inicio, fin = ip.parsear_periodo(date(2026, 10, 10), "00:00:00", date(2026, 10, 10), "23:59:59")
        self.assertEqual((inicio, fin), (INICIO, FIN))
        self.assertEqual(ip.periodo_key(inicio, fin), "2026-10-10_000000__2026-10-10_235959")
        for args in ((date(2026, 10, 10), "10:00:00", date(2026, 10, 10), "09:00:00"),
                     (date(2026, 10, 10), "10:00:00", date(2026, 10, 10), "10:00:00"),
                     (date(2026, 10, 10), "25:00", date(2026, 10, 11), "00:00:00")):
            with self.assertRaises(ValueError):
                ip.parsear_periodo(*args)

    # ---------- CSV ----------

    def test_R_S_T_U_csv_con_estados_de_adquisicion_y_sin_dno(self):
        ruta = self.poner_gpx(0)
        _, ev = BasePeriodo.evaluar(self, [
            entrada_manifiesto(llave(0), "DESCARGADO", ruta, "OFFLINE"),
            entrada_manifiesto(llave(1), "SIN_DATOS_PERIODO", actual="ONLINE"),
            entrada_manifiesto(llave(2), "SOLO_LOCAL")])
        texto = generar_csv(list(ev.values()))
        filas = {f["LLAVE"]: f for f in csv.DictReader(io.StringIO(texto))}
        self.assertIn("estado_datos", texto.splitlines()[0])  # R
        self.assertIn("estado_adquisicion", texto.splitlines()[0])  # S
        self.assertIn("estado_actual_baanool", texto.splitlines()[0])  # T
        self.assertNotIn("dno", texto.lower())  # U
        self.assertEqual((filas[llave(0)]["estado_datos"], filas[llave(0)]["estado_adquisicion"],
                          filas[llave(0)]["estado_actual_baanool"]), ("OK", "DESCARGADO", "OFFLINE"))
        self.assertEqual((filas[llave(1)]["estado_datos"], filas[llave(1)]["estado_adquisicion"],
                          filas[llave(1)]["estado_actual_baanool"], filas[llave(1)]["prioridad"]),
                         ("SIN_DATOS_PERIODO", "SIN_DATOS_PERIODO", "ONLINE", ""))
        self.assertEqual((filas[llave(2)]["estado_datos"], filas[llave(2)]["distancia_estimada_observada"]),
                         ("SIN_DISPOSITIVO_BAANOOL", ""))  # vacío, no 0
        self.assertNotIn("https://", texto)


class PruebasAdquisicion(BasePeriodo):
    def excel(self, n):
        return crear_excel(self.raiz, [(llave(i), None) for i in range(n)] + [("SOLO|LOCAL|X|1", None)],
                           con_dno=False)

    def adquirir(self, mapeo, sesion, **kwargs):
        excel = self.excel(3)
        with mock.patch.object(ip, "preparar_mapeo", lambda token, excel_, ses=None: (mapeo, 0)):
            return ip.actualizar_desde_baanool(INICIO, FIN, self.entrada, excel, self.raiz, self.manifiestos,
                                               sesion=sesion, token=TOKEN, pausar=lambda s: None, **kwargs)

    def test_adquisicion_completa_genera_manifiesto_y_resumen_sin_analizar(self):
        mapeo = mapeo_con(3, locales_extra=["SOLO|LOCAL|X|1"])
        eventos = []
        r = self.adquirir(mapeo, SesionLote(archivos={dno(1): b'<?xml version="1.0"?><gpx><trk><trkseg/></trk></gpx>'}),
                          on_progreso=lambda *a: eventos.append(a))
        self.assertTrue(r.ok)
        self.assertTrue(r.lote_completo)
        self.assertEqual(r.resumen["dispositivos_baanool"], 3)
        self.assertEqual(r.resumen["coincidencias_exactas"], 3)
        self.assertEqual(r.resumen["con_gpx"], 2)
        self.assertEqual(r.resumen["sin_datos_periodo"], 1)  # no cuenta como error
        self.assertEqual(r.resumen["errores"], 0)
        self.assertEqual(r.resumen["solo_local"], 1)
        self.assertTrue(ruta_manifiesto(INICIO, FIN, self.manifiestos).is_file())
        self.assertEqual(r.ruta_manifiesto, "salida/estado_descargas/" + nombre_manifiesto(INICIO, FIN))
        # Progreso: (actual, total, llave, estado); nunca DNO ni token.
        self.assertEqual([(a, t) for a, t, _, _ in eventos], [(1, 3), (2, 3), (3, 3)])
        self.assertNotIn(dno(0), repr(eventos))
        self.assertNotIn(TOKEN, repr(r))

    def test_rate_limit_devuelve_resultado_no_excepcion_y_guarda_manifiesto(self):
        sesion = SesionLote(exportaciones={dno(1): Resp(200, {"status": 5550, "resmsg": "Too many requests"})})
        r = self.adquirir(mapeo_con(3), sesion)
        self.assertFalse(r.ok)
        self.assertEqual(r.codigo_error, "RATE_LIMIT_BAANOOL")
        self.assertIn("limitó temporalmente", r.mensaje)
        self.assertFalse(r.lote_completo)
        self.assertTrue(ruta_manifiesto(INICIO, FIN, self.manifiestos).is_file())  # lo conocido se guarda
        self.assertEqual(r.resumen["no_procesadas"], 1)

    def test_token_invalido_devuelve_resultado(self):
        r = self.adquirir(mapeo_con(2), SesionLote(exportaciones={dno(0): Resp(401)}))
        self.assertEqual((r.ok, r.codigo_error), (False, "TOKEN_INVALIDO"))
        self.assertIn("renovarse", r.mensaje)

    def test_error_de_red_al_consultar_dispositivos(self):
        class SesionRota:
            def post(self, *a, **k):
                raise requests.exceptions.ConnectionError(f"sin red {TOKEN}")

        excel = self.excel(1)
        r = ip.actualizar_desde_baanool(INICIO, FIN, self.entrada, excel, self.raiz, self.manifiestos,
                                        sesion=SesionRota(), token=TOKEN)
        self.assertEqual((r.ok, r.codigo_error), (False, "ERROR_RED"))
        self.assertNotIn(TOKEN, repr(r))

    def test_periodo_invalido_no_consulta(self):
        r = ip.actualizar_desde_baanool(FIN, INICIO, self.entrada, self.excel(1), self.raiz, self.manifiestos,
                                        token=TOKEN)
        self.assertEqual(r.codigo_error, "PARAMETROS_INVALIDOS")

    def test_el_estado_de_sesion_solo_guarda_datos_no_sensibles(self):
        r = self.adquirir(mapeo_con(2), SesionLote())
        estado = {}
        ip.aplicar_resultado_adquisicion(estado, r)
        texto = repr(estado)
        self.assertNotIn(TOKEN, texto)
        self.assertNotIn(dno(0), texto)
        self.assertNotIn("https://", texto)
        self.assertEqual(set(estado), {"adquisicion"})  # no se tocó ninguna clave de análisis

    def test_adquirir_y_analizar_son_acciones_separadas(self):
        # Tras adquirir, el manifiesto existe pero NO hay resultados de análisis hasta analizar_periodo.
        mapeo = mapeo_con(2)
        r = self.adquirir(mapeo, SesionLote())
        estado = {}
        ip.aplicar_resultado_adquisicion(estado, r)
        for clave in ip.CLAVES_ANALISIS:
            self.assertNotIn(clave, estado)
        analisis = ip.analizar_periodo(INICIO, FIN, self.excel(2), self.raiz, ZONAS, self.manifiestos)
        self.assertEqual(len([e for e in analisis.evaluaciones if e.estado_datos == DATOS_OK]), 2)


class PruebaAppNoConsultaBaanool(unittest.TestCase):
    def test_abrir_la_app_no_hace_peticiones_ni_falla(self):
        from streamlit.testing.v1 import AppTest

        prohibido = mock.Mock(side_effect=AssertionError("la app no debe consultar BAANOOL al abrirse"))
        with mock.patch.object(requests.Session, "request", prohibido), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAANOOL_TOKEN", None)
            raiz = Path(__file__).resolve().parents[1]
            at = AppTest.from_file(str(raiz / "app.py"), default_timeout=120)
            at.run()
        self.assertEqual([e.value for e in at.exception], [])
        prohibido.assert_not_called()


if __name__ == "__main__":
    unittest.main()
