"""Detección de términos relacionados con drones sobre texto libre."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


def normalizar(texto: str | None) -> str:
    """Minúsculas, sin tildes y con todo lo que no sea letra o número como espacio.

    Así "Drones/RPAS" y "drones rpas" se comparan igual, y los patrones de
    config/terminos.json pueden escribirse sin preocuparse de tildes ni signos.
    """
    if not texto:
        return ""
    sin_tildes = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    limpio = re.sub(r"[^0-9a-zñ]+", " ", sin_tildes.lower())
    return " ".join(limpio.split())


@dataclass
class Termino:
    etiqueta: str
    categoria: str
    patron: re.Pattern
    excluir_si: list[str] = field(default_factory=list)

    def coincide(self, texto_normalizado: str) -> bool:
        if not self.patron.search(texto_normalizado):
            return False
        return not any(frase in texto_normalizado for frase in self.excluir_si)


@dataclass
class Diccionario:
    terminos: list[Termino]
    consultas_compra_agil: list[str]

    @classmethod
    def desde_archivo(cls, ruta: Path) -> "Diccionario":
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        terminos = [
            Termino(
                etiqueta=t["etiqueta"],
                categoria=t.get("categoria", "nucleo"),
                patron=re.compile(t["patron"]),
                excluir_si=[normalizar(f) for f in t.get("excluir_si", [])],
            )
            for t in datos["terminos"]
        ]
        return cls(terminos=terminos, consultas_compra_agil=datos.get("consultas_compra_agil", []))

    def buscar(self, *textos: str | None) -> list[str]:
        """Etiquetas de los términos presentes en cualquiera de los textos."""
        texto = " | ".join(normalizar(t) for t in textos if t)
        return [t.etiqueta for t in self.terminos if t.coincide(texto)]

    def categorias(self, etiquetas: list[str]) -> list[str]:
        por_etiqueta = {t.etiqueta: t.categoria for t in self.terminos}
        return sorted({por_etiqueta[e] for e in etiquetas if e in por_etiqueta})
