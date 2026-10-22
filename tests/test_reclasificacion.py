"""Pruebas del reclasificador de *.gpx.invalido históricos. Todo en directorios temporales.

    py -m unittest tests.test_reclasificacion -v
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.reclasificacion import (COLISION, CON_PUNTOS_VALIDOS, CONSERVAR, GPX_INVALIDO, MOVER, MOVIDO,
                                 SIN_DATOS_PERIODO_HISTORICO, clasificar_gpx_invalido, encontrar_gpx_invalidos,
                                 reclasificar)

NOMBRE = "baanool_2026-10-10_000000_2026-10-10_235959.gpx.invalido"
VACIO = b'<?xml version="1.0" encoding="UTF-8" standalone="no"?><gpx creator="www.coban.net"><trk><trkseg></trkseg></trk></gpx>'
VACIO_NS = b'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg/></trk></gpx>'
CORRUPTO = b"<?xml version='1.0'?><gpx><trk><trkseg><trkpt lat='19.3'"
NO_GPX = b"<html><body>Error</body></html>"
TRKPT_INVALIDOS = (b'<?xml version="1.0"?><gpx><trk><trkseg>'
                   b'<trkpt lat="abc" lon="-99.1"><time>2026-10-10 00:00:00</time></trkpt>'
                   b'<trkpt lat="19.3" lon="-99.1"></trkpt></trkseg></trk></gpx>')
CON_PUNTOS = (b'<?xml version="1.0"?><gpx><trk><trkseg>'
              b'<trkpt lat="19.3" lon="-99.1"><ele>0.0</ele><time>2026-10-10 00:00:00</time></trkpt>'
              b'</trkseg></trk></gpx>')
GPX_NORMAL = CON_PUNTOS


def instantanea(*raices: Path) -> dict[str, tuple[str, int]]:
    """{ruta -> (sha256, mtime_ns)} de todos los archivos bajo las raíces."""
    resultado = {}
    for raiz in raices:
        for p in sorted(raiz.rglob("*")):
            if p.is_file():
                resultado[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return resultado


class PruebasReclasificacion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.entrada = base / "entrada"
        self.destino = base / "salida" / "diagnostico_gpx_sin_datos"
        self.entrada.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def poner(self, carpeta: str, nombre: str, contenido: bytes) -> Path:
        d = self.entrada / carpeta
        d.mkdir(exist_ok=True)
        ruta = d / nombre
        ruta.write_bytes(contenido)
        return ruta

    def escenario(self):
        """Dos vehículos sin posiciones, uno corrupto, uno con trkpt inválidos y varios GPX normales."""
        return {
            "vacio_1": self.poner("AVEO - 2022 - VINO - P62BHW", NOMBRE, VACIO),
            "vacio_2": self.poner("MARCH - 2019 - TURQ - MAY737C", NOMBRE, VACIO_NS),
            "corrupto": self.poner("RIO - 2017 - ROJO - MYE912A", NOMBRE, CORRUPTO),
            "trkpt_invalidos": self.poner("BEAT - 2020 - GRIS - NFN014B", NOMBRE, TRKPT_INVALIDOS),
            "normal": self.poner("AVEO - 2018 - ARENA - F86AYT", "baanool_2026-10-10_000000_2026-10-10_235959.gpx",
                                 GPX_NORMAL),
            "manual": self.poner("AVEO - 2018 - ARENA - F86AYT", "F86AYT.gpx", GPX_NORMAL),
            "otro_tipo": self.poner("AVEO - 2018 - ARENA - F86AYT", "notas.txt", b"x"),
        }

    # ---------- clasificación ----------

    def test_A_gpx_valido_con_cero_trkpt_es_sin_datos_periodo_historico(self):
        for contenido in (VACIO, VACIO_NS):
            ruta = self.poner("V1", NOMBRE, contenido)
            self.assertEqual(clasificar_gpx_invalido(ruta)[0], SIN_DATOS_PERIODO_HISTORICO)

    def test_B_xml_corrupto_o_no_gpx_permanece_invalido(self):
        for contenido in (CORRUPTO, NO_GPX, b"", b"no es xml"):
            ruta = self.poner("V1", NOMBRE, contenido)
            self.assertEqual(clasificar_gpx_invalido(ruta)[0], GPX_INVALIDO, contenido)

    def test_C_trkpt_todos_invalidos_permanece_invalido(self):
        ruta = self.poner("V1", NOMBRE, TRKPT_INVALIDOS)
        clase, detalle = clasificar_gpx_invalido(ruta)
        self.assertEqual(clase, GPX_INVALIDO)
        self.assertIn("ninguno es válido", detalle)

    def test_con_puntos_validos_es_inesperado_y_no_se_mueve(self):
        ruta = self.poner("V1", NOMBRE, CON_PUNTOS)
        self.assertEqual(clasificar_gpx_invalido(ruta)[0], CON_PUNTOS_VALIDOS)
        items = reclasificar(self.entrada, self.destino, dry_run=False)
        self.assertEqual(items[0].accion, CONSERVAR)
        self.assertTrue(ruta.exists())

    def test_solo_se_consideran_archivos_gpx_invalido(self):
        archivos = self.escenario()
        self.poner("AVEO - 2018 - ARENA - F86AYT", "x.gpx.descarga", VACIO)
        self.poner("AVEO - 2018 - ARENA - F86AYT", "x.gpx.descarga.invalido", VACIO)
        self.poner("AVEO - 2018 - ARENA - F86AYT", "otro.txt.invalido", VACIO)
        nombres = sorted(p.parent.name for p in encontrar_gpx_invalidos(self.entrada))
        self.assertEqual(nombres, sorted(["AVEO - 2022 - VINO - P62BHW", "MARCH - 2019 - TURQ - MAY737C",
                                          "RIO - 2017 - ROJO - MYE912A", "BEAT - 2020 - GRIS - NFN014B"]))

    # ---------- dry-run y ejecución real ----------

    def test_D_dry_run_no_mueve_ni_modifica_nada(self):
        self.escenario()
        antes = instantanea(self.entrada)
        items = reclasificar(self.entrada, self.destino, dry_run=True)
        self.assertEqual(instantanea(self.entrada), antes)
        self.assertFalse(self.destino.exists())  # ni siquiera crea la carpeta diagnóstica
        acciones = {i.carpeta: i.accion for i in items}
        self.assertEqual(acciones["AVEO - 2022 - VINO - P62BHW"], MOVER)
        self.assertEqual(acciones["MARCH - 2019 - TURQ - MAY737C"], MOVER)
        self.assertEqual(acciones["RIO - 2017 - ROJO - MYE912A"], CONSERVAR)
        self.assertEqual(acciones["BEAT - 2020 - GRIS - NFN014B"], CONSERVAR)
        self.assertEqual(items[0].destino, self.destino / items[0].carpeta / NOMBRE)  # destino propuesto

    def test_E_ejecucion_real_mueve_solo_los_vacios_validos(self):
        a = self.escenario()
        reclasificar(self.entrada, self.destino, dry_run=False)
        self.assertFalse(a["vacio_1"].exists() or a["vacio_2"].exists())
        self.assertTrue((self.destino / a["vacio_1"].parent.name / NOMBRE).is_file())
        self.assertTrue((self.destino / a["vacio_2"].parent.name / NOMBRE).is_file())
        self.assertTrue(a["corrupto"].exists())  # el realmente inválido sigue donde estaba
        self.assertTrue(a["trkpt_invalidos"].exists())
        self.assertEqual(len([p for p in self.destino.rglob("*") if p.is_file()]), 2)

    def test_F_los_gpx_validos_y_otros_archivos_no_se_tocan(self):
        a = self.escenario()
        intocables = [a["normal"], a["manual"], a["otro_tipo"]]
        antes = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in intocables}
        reclasificar(self.entrada, self.destino, dry_run=False)
        for p, (contenido, mtime) in antes.items():
            self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), (contenido, mtime))

    def test_G_nunca_se_borra_ningun_archivo(self):
        self.escenario()
        total_antes = len([p for p in self.entrada.rglob("*") if p.is_file()])
        contenidos_antes = sorted(p.read_bytes() for p in self.entrada.rglob("*") if p.is_file())
        reclasificar(self.entrada, self.destino, dry_run=False)
        todos = [p for raiz in (self.entrada, self.destino) for p in raiz.rglob("*") if p.is_file()]
        self.assertEqual(len(todos), total_antes)
        self.assertEqual(sorted(p.read_bytes() for p in todos), contenidos_antes)

    def test_H_colision_en_destino_no_sobrescribe(self):
        a = self.escenario()
        previo = self.destino / a["vacio_1"].parent.name / NOMBRE
        previo.parent.mkdir(parents=True)
        previo.write_bytes(b"EVIDENCIA PREVIA")
        items = reclasificar(self.entrada, self.destino, dry_run=False)
        self.assertEqual(previo.read_bytes(), b"EVIDENCIA PREVIA")  # intacto
        self.assertTrue(a["vacio_1"].exists())  # el origen tampoco se movió
        colisiones = [i for i in items if i.accion == COLISION]
        self.assertEqual(len(colisiones), 1)
        self.assertIn("ya existe", colisiones[0].detalle)
        self.assertEqual(sorted(p.name for p in previo.parent.iterdir()), [NOMBRE])  # sin sufijos inventados
        # El otro vacío sí se movió.
        self.assertEqual([i.accion for i in items if i.carpeta == a["vacio_2"].parent.name], [MOVIDO])

    def test_I_conserva_la_asociacion_con_la_carpeta_de_la_llave(self):
        a = self.escenario()
        reclasificar(self.entrada, self.destino, dry_run=False)
        movido = self.destino / "AVEO - 2022 - VINO - P62BHW" / NOMBRE
        self.assertTrue(movido.is_file())
        self.assertEqual(movido.parent.name, a["vacio_1"].parent.name)  # misma carpeta segura de la LLAVE
        self.assertEqual(movido.name, NOMBRE)  # el nombre conserva el periodo

    def test_segunda_ejecucion_no_hace_nada(self):
        self.escenario()
        reclasificar(self.entrada, self.destino, dry_run=False)
        antes = instantanea(self.entrada, self.destino)
        items = reclasificar(self.entrada, self.destino, dry_run=False)
        self.assertEqual(instantanea(self.entrada, self.destino), antes)
        self.assertTrue(all(i.accion == CONSERVAR for i in items))

    def test_mismo_nombre_en_distintos_vehiculos_no_colisiona(self):
        # Todos los archivos históricos se llaman igual: la subcarpeta del vehículo lo evita.
        a = self.escenario()
        items = reclasificar(self.entrada, self.destino, dry_run=False)
        self.assertEqual(len([i for i in items if i.accion == MOVIDO]), 2)
        self.assertEqual(len([i for i in items if i.accion == COLISION]), 0)


if __name__ == "__main__":
    unittest.main()
