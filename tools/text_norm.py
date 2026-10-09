"""Normalización de texto a unidades de coincidencia.

Guion y transcripción pasan por la misma función (`to_units`), así que los
números escritos en dígitos y en palabras coinciden en ambos sentidos.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from num2words import num2words

# Corridas de letras o de dígitos: equivale a extraer `[^\W_]+` y cortar cada
# corrida en la frontera entre letras y dígitos ("5070ti" da "5070", "ti").
_RUN_RE = re.compile(r"[^\W\d_]+|\d+")

# Corridas de dígitos más largas que esto no se convierten a palabras.
MAX_NUM_DIGITS = 12

# Número con separador de miles a la española: punto, espacio duro (U+00A0),
# espacio fino duro (U+202F) o espacio fino (U+2009) entre grupos exactos de
# tres dígitos ("10.000", "1.000.000", "2 500"). Se une antes de convertir para
# que coincida con "diez mil" o "un millón". La coma no separa miles (en
# español es decimal) y "1.5" o "3.14" no tienen grupos de tres: no se tocan.
_THOUSANDS_RE = re.compile(
    r"(?<![\d.,])[1-9]\d{0,2}(?:[.   ]\d{3})+(?!\d|\.\d)")
_THOUSANDS_SEP_RE = re.compile(r"[.   ]")

# Palabras funcionales del español ya plegadas con `fold` (sin tildes).
# Solo palabras sin contenido propio: el matcher les da menos peso.
# "no" queda fuera a propósito: en un guion puede cambiar el sentido.
STOPWORDS_ES: frozenset[str] = frozenset({
    # Artículos y contracciones
    "el", "la", "lo", "los", "las", "un", "una", "unos", "unas", "al", "del",
    # Preposiciones
    "a", "ante", "con", "contra", "de", "desde", "durante", "en", "entre",
    "hacia", "hasta", "mediante", "para", "por", "segun", "sin", "sobre",
    "tras",
    # Conjunciones y relativos
    "y", "e", "o", "u", "ni", "que", "pero", "sino", "si", "porque", "pues",
    "aunque", "como", "cuando", "donde",
    # Pronombres átonos
    "me", "te", "se", "nos", "os", "le", "les",
    # Posesivos
    "mi", "mis", "tu", "tus", "su", "sus",
    "nuestro", "nuestra", "nuestros", "nuestras",
    "vuestro", "vuestra", "vuestros", "vuestras",
    # Auxiliares muy frecuentes
    "es", "son", "fue", "era", "ha", "he", "has", "han", "hemos",
})


class _StripMn(dict):
    """Tabla de `str.translate` que borra las marcas Mn; se llena al vuelo."""

    def __missing__(self, cp: int) -> int | None:
        value = None if unicodedata.category(chr(cp)) == "Mn" else cp
        if len(self) < 65536:  # acota la memoria ante textos exóticos
            self[cp] = value
        return value


_STRIP_MN = _StripMn()


def fold(s: str) -> str:
    """NFKD, quita marcas Mn y pasa a minúsculas (no toca dígitos ni signos)."""
    s = unicodedata.normalize("NFKD", s)
    if not s.isascii():
        s = s.translate(_STRIP_MN)
    return s.lower()


@lru_cache(maxsize=4096)
def _number_units(digits: str) -> tuple[str, ...]:
    """Unidades de una corrida de dígitos ("5070" da cinco, mil, setenta)."""
    if len(digits) > MAX_NUM_DIGITS:
        return (digits,)
    words = num2words(int(digits), lang="es")
    return tuple(_RUN_RE.findall(fold(words)))


def to_units(s: str) -> list[str]:
    """Unidades de coincidencia de un texto cualquiera (guion o transcripción).

    Los signos, guiones, barras y emojis separan unidades y no producen
    ninguna: "RTX-5070" da ["rtx", "cinco", "mil", "setenta"] y un guion
    largo suelto (U+2014) da []. Los separadores de miles se unen antes:
    "10.000" da ["diez", "mil"], igual que "10000".
    """
    # Antes de `fold`: NFKD convierte el espacio duro en un espacio normal.
    s = _THOUSANDS_RE.sub(lambda m: _THOUSANDS_SEP_RE.sub("", m.group()), s)
    units: list[str] = []
    for run in _RUN_RE.findall(fold(s)):
        # Las corridas de `\d+` son todas Nd, igual que `str.isdecimal`.
        if run[0].isdecimal():
            units.extend(_number_units(run))
        else:
            units.append(run)
    return units


def is_stopword(unit: str) -> bool:
    """True si la unidad (ya normalizada) es una palabra funcional."""
    return unit in STOPWORDS_ES
