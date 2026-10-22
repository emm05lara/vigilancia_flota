"""Mapeo en memoria: LLAVE (Lista_Vehiculos.xlsx) <-> dispositivos de BAANOOL.

                    LLAVE
                   /     \\
       carpeta local       DNO
             |              |
             v              v
            GPX          BAANOOL

La LLAVE es la identidad operativa local; el DNO es solo el identificador técnico de BAANOOL.
El mapeo se construye cada vez que se consulta BAANOOL: no se guarda y NO se escribe en el Excel.

Regla principal: coincidencia EXACTA entre la LLAVE y el `name` del dispositivo (solo espacios
externos ya limpiados; sin cambiar mayúsculas, separadores ni componentes, sin fuzzy matching).

Estados:
- COINCIDENCIA_EXACTA: LLAVE == name, sin ningún problema. Es lo único que asocia un DNO.
- POSIBLE_COINCIDENCIA: diagnóstico para revisión manual (separador, mayúsculas, espacios, o
  placa final == carNum). NUNCA asigna DNO automáticamente.
- SOLO_LOCAL: LLAVE sin dispositivo. No se inventa ningún DNO.
- SOLO_BAANOOL: dispositivo sin LLAVE local. No se crea ninguna LLAVE.
- CONFLICTO: ambigüedad que no se resuelve eligiendo uno (duplicados, name o dno vacíos o inválidos).
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .baanool import (DNO_INVALIDO, DNO_VACIO, NAME_INVALIDO, NAME_VACIO, DispositivoBaanool,
                      enmascarar_dno)

COINCIDENCIA_EXACTA = "COINCIDENCIA_EXACTA"
POSIBLE_COINCIDENCIA = "POSIBLE_COINCIDENCIA"
SOLO_LOCAL = "SOLO_LOCAL"
SOLO_BAANOOL = "SOLO_BAANOOL"
CONFLICTO = "CONFLICTO"
ESTADOS = [COINCIDENCIA_EXACTA, POSIBLE_COINCIDENCIA, SOLO_LOCAL, SOLO_BAANOOL, CONFLICTO]

_SEPARADORES = re.compile(r"[/\\]")
_ESPACIOS_ALREDEDOR = re.compile(r"\s*\|\s*")


@dataclass(frozen=True)
class EntradaMapeo:
    estado: str
    motivo: str
    llave: str | None = None  # None si la entrada es solo de BAANOOL
    dispositivos: tuple[DispositivoBaanool, ...] = ()  # asociado (exacta) o involucrados/candidatos

    @property
    def dispositivo(self) -> DispositivoBaanool | None:
        """El dispositivo asociado: solo existe en una coincidencia exacta."""
        return self.dispositivos[0] if self.estado == COINCIDENCIA_EXACTA else None

    @property
    def dno(self) -> str | None:
        """DNO asignado: solo en coincidencia exacta (nunca en una posible)."""
        return self.dispositivo.dno if self.dispositivo else None

    @property
    def etiqueta(self) -> str:
        if self.llave:
            return self.llave
        nombres = sorted({d.name for d in self.dispositivos if d.name})
        return " / ".join(nombres) if nombres else "(sin name)"


@dataclass
class ResultadoMapeoBaanool:
    locales: list[str]  # LLAVEs únicas del Excel
    dispositivos: list[DispositivoBaanool]
    entradas: list[EntradaMapeo]

    def con_estado(self, estado: str) -> list[EntradaMapeo]:
        return [e for e in self.entradas if e.estado == estado]

    @property
    def exactas(self) -> dict[str, DispositivoBaanool]:
        """{llave -> dispositivo}: lo único que podría usarse para una descarga posterior."""
        return {e.llave: e.dispositivo for e in self.entradas if e.estado == COINCIDENCIA_EXACTA}

    @property
    def excepciones(self) -> list[EntradaMapeo]:
        return [e for e in self.entradas if e.estado != COINCIDENCIA_EXACTA]

    def resumen(self) -> dict[str, int]:
        cuenta = Counter(e.estado for e in self.entradas)
        return {"locales": len(self.locales), "dispositivos": len(self.dispositivos),
                "exactas": cuenta[COINCIDENCIA_EXACTA], "posibles": cuenta[POSIBLE_COINCIDENCIA],
                "solo_local": cuenta[SOLO_LOCAL], "solo_baanool": cuenta[SOLO_BAANOOL],
                "conflictos": cuenta[CONFLICTO]}


# ---------- diagnósticos de parecido (nunca asignan nada) ----------

def diferencias_con_name(llave: str, name: str) -> list[str]:
    """Motivos por los que LLAVE y name serían iguales salvo detalles de escritura; [] si no lo son.

    Es solo diagnóstico: nunca convierte una diferencia en coincidencia.
    """
    if not llave or not name or llave == name:
        return []
    SEPARADOR = "diferencia de separador ('/' vs '|')"
    ESPACIOS = "diferencia de espacios alrededor del separador"
    MAYUSCULAS = "diferencia de mayúsculas/minúsculas"

    a = _SEPARADORES.sub("|", llave)  # '/' o '\' tratados como '|' solo para comparar
    motivos = [SEPARADOR] if a != llave else []
    if a == name:
        return motivos
    if a.casefold() == name.casefold():
        return motivos + [MAYUSCULAS]
    a2, b2 = _ESPACIOS_ALREDEDOR.sub("|", a), _ESPACIOS_ALREDEDOR.sub("|", name)
    if a2 == b2:
        return motivos + [ESPACIOS]
    if a2.casefold() == b2.casefold():
        return motivos + [ESPACIOS, MAYUSCULAS]
    return []


def placa_de_llave(llave: str) -> str:
    return re.split(r"[|/\\]", llave)[-1].strip()


def _coincide_placa(llave: str, car_num: str | None) -> bool:
    placa = placa_de_llave(llave)
    return bool(placa and car_num and placa.casefold() == car_num.strip().casefold())


# ---------- mapeo ----------

def _problemas(d: DispositivoBaanool, nombres: Counter, dnos: Counter) -> list[str]:
    texto = []
    if NAME_VACIO in d.problemas:
        texto.append("name vacío")
    if NAME_INVALIDO in d.problemas:
        texto.append("name inválido")
    if DNO_VACIO in d.problemas:
        texto.append("dno vacío")
    if DNO_INVALIDO in d.problemas:
        texto.append("dno inválido")
    if d.name and nombres[d.name] > 1:
        texto.append(f"name duplicado en BAANOOL ({nombres[d.name]} dispositivos)")
    if d.dno and dnos[d.dno] > 1:
        texto.append(f"dno duplicado en BAANOOL ({dnos[d.dno]} dispositivos, {enmascarar_dno(d.dno)})")
    return texto


def mapear_llaves_con_dispositivos(llaves: list[str], dispositivos: list[DispositivoBaanool]
                                   ) -> ResultadoMapeoBaanool:
    """Cada LLAVE única produce una entrada; cada dispositivo aparece en una sola entrada."""
    limpias = [str(l).strip() for l in llaves if l is not None and str(l).strip()]
    veces = Counter(limpias)
    locales = list(dict.fromkeys(limpias))

    nombres = Counter(d.name for d in dispositivos if d.name)
    dnos = Counter(d.dno for d in dispositivos if d.dno)
    por_name: dict[str, list[DispositivoBaanool]] = defaultdict(list)
    for d in dispositivos:
        if d.name:
            por_name[d.name].append(d)

    entradas: list[EntradaMapeo] = []
    consumidos: set[int] = set()  # dispositivos ya explicados por una LLAVE (exacta o conflicto)
    candidatos: set[int] = set()  # dispositivos que son posible coincidencia de alguna LLAVE

    for llave in locales:
        encontrados = por_name.get(llave, [])
        if veces[llave] > 1:
            consumidos.update(d.indice for d in encontrados)
            entradas.append(EntradaMapeo(CONFLICTO, f"LLAVE repetida {veces[llave]} veces en el Excel",
                                         llave, tuple(encontrados)))
        elif len(encontrados) > 1:
            consumidos.update(d.indice for d in encontrados)
            entradas.append(EntradaMapeo(CONFLICTO, f"name duplicado en BAANOOL ({len(encontrados)} "
                                                    "dispositivos); no se elige ninguno", llave,
                                         tuple(encontrados)))
        elif len(encontrados) == 1:
            d = encontrados[0]
            consumidos.add(d.indice)
            problemas = _problemas(d, nombres, dnos)
            if problemas:
                entradas.append(EntradaMapeo(CONFLICTO, "; ".join(problemas), llave, (d,)))
            else:
                entradas.append(EntradaMapeo(COINCIDENCIA_EXACTA, "LLAVE == name", llave, (d,)))
        else:
            razones: dict[int, list[str]] = {}
            for d in dispositivos:
                motivos = diferencias_con_name(llave, d.name)
                if _coincide_placa(llave, d.car_num):
                    motivos = motivos + ["la placa final de la LLAVE coincide con carNum"]
                if motivos:
                    razones[d.indice] = motivos
            if razones:
                candidatos.update(razones)
                elegidos = tuple(d for d in dispositivos if d.indice in razones)
                detalle = "; ".join(f"{d.name or '(sin name)'}: {', '.join(razones[d.indice])}"
                                    for d in elegidos)
                entradas.append(EntradaMapeo(
                    POSIBLE_COINCIDENCIA, f"{detalle}. Requiere revisión manual; no se asigna DNO",
                    llave, elegidos))
            else:
                entradas.append(EntradaMapeo(SOLO_LOCAL, "ningún dispositivo de BAANOOL tiene ese name",
                                             llave))

    reportados: set[int] = set()
    for d in dispositivos:
        if d.indice in consumidos or d.indice in reportados:
            continue
        problemas = _problemas(d, nombres, dnos)
        if problemas:  # conflicto propio del dispositivo; se agrupan los que comparten name o dno
            grupo = [x for x in dispositivos if x.indice not in consumidos and (
                x.indice == d.indice or (d.name and x.name == d.name) or (d.dno and x.dno == d.dno))]
            reportados.update(x.indice for x in grupo)
            entradas.append(EntradaMapeo(CONFLICTO, "; ".join(problemas), None, tuple(grupo)))
        elif d.indice in candidatos:
            continue  # ya aparece como candidato de una POSIBLE_COINCIDENCIA
        else:
            reportados.add(d.indice)
            entradas.append(EntradaMapeo(SOLO_BAANOOL, "su name no aparece en Lista_Vehiculos.xlsx",
                                         None, (d,)))
    return ResultadoMapeoBaanool(locales, list(dispositivos), entradas)
