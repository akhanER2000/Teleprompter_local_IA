"""Normalización a unidades de coincidencia (docs/diseno_v2.md, sección 3)."""
from __future__ import annotations

import re

import pytest

from tools.text_norm import STOPWORDS_ES, fold, is_stopword, to_units

EM_DASH = "\u2014"
EN_DASH = "\u2013"
ROCKET = "\U0001F680"
WAVE_SKIN = "\U0001F44B\U0001F3FD"  # emoji con modificador de tono
UNIT_RE = re.compile(r"^[^\W_]+$")


# ---------------------------------------------------------------- fold

@pytest.mark.parametrize("raw,expected", [
    ("Ñandú", "nandu"),
    ("CANCIÓN", "cancion"),
    ("pingüino", "pinguino"),
    ("1º 2ª", "1o 2a"),            # formas de compatibilidad
    ("x²", "x2"),
    ("\ufb01n", "fin"),            # ligadura fi
    ("\uff32\uff34\uff38\uff15\uff10", "rtx50"),  # ancho completo
    ("\u0130stanbul", "istanbul"),  # I con punto: sin marca combinante residual
])
def test_fold_pliega_tildes_compatibilidad_y_mayusculas(raw: str, expected: str) -> None:
    assert fold(raw) == expected


def test_fold_no_toca_digitos_ni_signos() -> None:
    assert fold("RTX-5070, ¿sí? 3/4 ...") == "rtx-5070, ¿si? 3/4 ..."
    assert fold(EM_DASH) == EM_DASH
    assert fold("") == ""


# ---------------------------------------------------------------- to_units

@pytest.mark.parametrize("raw,expected", [
    ("RTX-5070", ["rtx", "cinco", "mil", "setenta"]),
    ("uno/dos", ["uno", "dos"]),
    ("e-mail", ["e", "mail"]),
    ("2026", ["dos", "mil", "veintiseis"]),
    ("21", ["veintiuno"]),
    ("5070ti", ["cinco", "mil", "setenta", "ti"]),
    ("¿Funciona?", ["funciona"]),
    ("0", ["cero"]),
    ("007", ["siete"]),
    ("99", ["noventa", "y", "nueve"]),
    ("100", ["cien"]),
    ("16", ["dieciseis"]),
    ("1000000", ["un", "millon"]),
    ("x²", ["x", "dos"]),
    ("1º", ["uno", "o"]),
    ("\uff32\uff34\uff38\uff15\uff10\uff17\uff10", ["rtx", "cinco", "mil", "setenta"]),
    ("foo_bar", ["foo", "bar"]),       # el guion bajo también separa
    ("Mañana", ["manana"]),
    ("Genial" + ROCKET, ["genial"]),
])
def test_to_units_casos(raw: str, expected: list[str]) -> None:
    assert to_units(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", EM_DASH, EN_DASH, "...", "¿", "?", "¡!", "-", "/", "_", "[", "]",
    ROCKET, WAVE_SKIN, "\u2026", "\u00ab\u00bb", "\"'", "\ufe0f",
])
def test_to_units_signos_y_emojis_no_producen_unidades(raw: str) -> None:
    assert to_units(raw) == []


def test_to_units_frase_completa() -> None:
    assert to_units("Hola, ¿cómo estás? Bien\u2026 gracias.") == [
        "hola", "como", "estas", "bien", "gracias",
    ]


def test_to_units_corridas_largas_de_digitos_quedan_como_estan() -> None:
    assert to_units("1234567890123") == ["1234567890123"]  # 13 dígitos
    doce = to_units("999999999999")  # 12 dígitos: se convierte
    assert doce and all(u.isalpha() for u in doce)
    assert doce[:3] == ["novecientos", "noventa", "y"]


def test_to_units_unidades_ya_normalizadas() -> None:
    muestra = (
        "Ñandú RTX-5070 ¿Funciona? e-mail uno/dos 2026 x² 1º \ufb01n "
        + EM_DASH + " ... " + ROCKET + " canción ÁRBOL pingüino 5070ti"
    )
    units = to_units(muestra)
    assert units
    for u in units:
        assert UNIT_RE.match(u), u
        assert fold(u) == u
        assert to_units(u) == [u] or u.isdecimal()


def test_to_units_devuelve_una_lista_nueva_en_cada_llamada() -> None:
    primera = to_units("5070")
    primera.append("basura")
    assert to_units("5070") == ["cinco", "mil", "setenta"]


@pytest.mark.parametrize("digitos,palabras", [
    ("RTX 5070 Ti", "rtx cinco mil setenta ti"),
    ("en 2026", "en dos mil veintiséis"),
    ("las 21 fases", "las veintiuno fases"),
    ("RTX-5070", "RTX cinco mil setenta"),
    ("tenemos 10.000 usuarios", "tenemos diez mil usuarios"),
    ("cuesta 2.500 euros", "cuesta dos mil quinientos euros"),
    ("ya somos 1.000.000", "ya somos un millón"),
])
def test_digitos_y_palabras_coinciden_en_ambos_sentidos(digitos: str, palabras: str) -> None:
    assert to_units(digitos) == to_units(palabras)


# ---------------------------------------------------------------- separadores de miles

@pytest.mark.parametrize("raw,expected", [
    ("10.000", ["diez", "mil"]),
    ("2.500", ["dos", "mil", "quinientos"]),
    ("1.000.000", ["un", "millon"]),
    ("10 000", ["diez", "mil"]),         # espacio duro
    ("2 500", ["dos", "mil", "quinientos"]),  # espacio fino duro
    ("(10.000)", ["diez", "mil"]),
    ("10.000.", ["diez", "mil"]),             # punto final de frase
    ("1.000,50", ["mil", "cincuenta"]),       # coma decimal tras los miles
])
def test_separadores_de_miles_se_unen(raw: str, expected: list[str]) -> None:
    assert to_units(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1.5", ["uno", "cinco"]),                # no son grupos de tres
    ("3.14", ["tres", "catorce"]),
    ("12.03.2024", ["doce", "tres", "dos", "mil", "veinticuatro"]),
    ("192.168.1.1", ["ciento", "noventa", "y", "dos", "ciento", "sesenta", "y", "ocho",
                     "uno", "uno"]),
    ("10,000", ["diez", "cero"]),             # la coma es decimal en español
    ("0.500", ["cero", "quinientos"]),        # un grupo no empieza en cero
    ("1.0000", ["uno", "cero"]),              # cuatro dígitos: no es un grupo
    ("10 000", ["diez", "cero"]),             # el espacio normal separa palabras
])
def test_lo_que_no_es_separador_de_miles_no_se_une(raw: str, expected: list[str]) -> None:
    assert to_units(raw) == expected


# ---------------------------------------------------------------- stopwords

def test_stopwords_es_frozenset_de_unidades_normalizadas() -> None:
    assert isinstance(STOPWORDS_ES, frozenset)
    assert STOPWORDS_ES
    for w in STOPWORDS_ES:
        assert to_units(w) == [w], w


def test_stopwords_es_incluye_palabras_funcionales() -> None:
    esperadas = {
        "el", "la", "los", "las", "un", "una", "al", "del",  # artículos y contracciones
        "a", "de", "en", "con", "por", "para", "sin", "desde", "hasta",  # preposiciones
        "y", "e", "o", "u", "ni", "que", "pero",  # conjunciones
        "me", "te", "se", "nos", "lo", "le", "les",  # pronombres átonos
        "mi", "tu", "su", "sus",  # posesivos
        "es", "son", "fue", "ha", "he",  # auxiliares
    }
    assert esperadas <= STOPWORDS_ES


@pytest.mark.parametrize("palabra", [
    "no", "hola", "sistema", "fase", "voz", "gracias", "mundo", "tarjeta",
    "rtx", "cinco", "mil", "setenta", "uno", "dos", "funciona",
])
def test_stopwords_es_excluye_no_y_palabras_de_contenido(palabra: str) -> None:
    assert palabra not in STOPWORDS_ES
    assert not is_stopword(palabra)


def test_is_stopword_sobre_unidades_normalizadas() -> None:
    assert is_stopword("de")
    assert is_stopword(to_units("Según")[0])
    assert is_stopword(to_units("Él")[0])
    assert not is_stopword("Según")  # espera unidades ya normalizadas
    assert not is_stopword("")
