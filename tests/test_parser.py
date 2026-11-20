"""Parser del guion (docs/diseno_v2.md, sección 4).

El frontend dibuja los `segments` 1:1, así que el invariante central es que
los segmentos `word` aparecen en orden de índice 0..n-1 y que
`tokens[i].idx == i ==` el índice del i-ésimo segmento `word`.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from pathlib import Path

import pytest

from tools.script_matcher import ScriptMatcher
from tools.script_parser import MAX_SCRIPT_CHARS, ParsedScript, Token, parse_script
from tools.text_norm import to_units

ROOT = Path(__file__).resolve().parent.parent
EJEMPLO = ROOT / "guiones" / "ejemplo.txt"

EM_DASH = "\u2014"
ROCKET = "\U0001F680"
WAVE_SKIN = "\U0001F44B\U0001F3FD"

LINE = ("break", "line")
PARA = ("break", "paragraph")


def d(text: str, at_idx: int) -> tuple[str, str, int]:
    return ("dir", text, at_idx)


def shape(p: ParsedScript) -> list:
    """Vista compacta: palabras como str; directivas y saltos como tuplas."""
    out: list = []
    for s in p.segments:
        if s["type"] == "word":
            out.append(s["text"])
        elif s["type"] == "directive":
            out.append(d(s["text"], s["at_idx"]))
        else:
            out.append(("break", s["kind"]))
    return out


def word_segments(p: ParsedScript) -> list[dict]:
    return [s for s in p.segments if s["type"] == "word"]


def check_invariants(p: ParsedScript) -> None:
    """Invariantes que el frontend y el matcher dan por ciertos."""
    words = word_segments(p)
    assert p.total_tokens == len(p.tokens) == len(words)
    assert [s["idx"] for s in words] == list(range(p.total_tokens))
    for i, (tok, seg) in enumerate(zip(p.tokens, words)):
        assert isinstance(tok, Token)
        assert tok.idx == i == seg["idx"]
        assert tok.text == seg["text"]
        assert tok.text and not re.search(r"\s", tok.text)
        assert isinstance(tok.units, list)
        assert tok.units == to_units(tok.text)
        if tok.units:
            assert set(seg) == {"type", "idx", "text"}
        else:
            assert set(seg) == {"type", "idx", "text", "skip"}
            assert seg["skip"] is True

    words_before = 0
    prev_type = None
    for pos, s in enumerate(p.segments):
        t = s["type"]
        assert t in {"word", "directive", "ref", "break"}
        if t == "word":
            words_before += 1
        elif t == "ref":
            # Hito 85: misma regla de at_idx que una directiva; no da tokens ni corta el renglón.
            expected = words_before
            while expected < p.total_tokens and not p.tokens[expected].units:
                expected += 1
            assert s["at_idx"] == expected
            assert s["kind"] in {"img", "ppt", "pdf", "code", "nota"}
            assert s["text"].startswith("[") and s["text"].endswith("]")
            assert s["label"].startswith(f"{s['kind']}: ")
            continue   # no cuenta como vecino de un break
        elif t == "directive":
            assert set(s) == {"type", "text", "at_idx"}
            # La primera palabra con unidades que la sigue (o el total): la
            # posición nunca se detiene en un token sin unidades.
            expected = words_before
            while expected < p.total_tokens and not p.tokens[expected].units:
                expected += 1
            assert s["at_idx"] == expected
            assert s["text"].startswith("[") and s["text"].endswith("]")
            assert "  " not in s["text"]
            assert not re.search(r"[^\S ]", s["text"])  # solo espacios simples
        else:
            assert set(s) == {"type", "kind"}
            assert s["kind"] in {"line", "paragraph"}
            assert 0 < pos < len(p.segments) - 1, "break al principio o al final"
            assert prev_type != "break", "dos break seguidos"
            vecinos = {p.segments[pos - 1]["type"], p.segments[pos + 1]["type"]}
            if "directive" in vecinos:
                assert s["kind"] == "paragraph"
        prev_type = t

    assert not re.search("[\r\x0b\x0c\x85  ]", p.text)
    assert not re.search("[­​‎‏⁠﻿]", p.text)
    p.text.encode("utf-8")  # sin sustitutos sueltos: siempre se puede enviar
    assert re.fullmatch(r"[0-9a-f]{32}", p.script_id)
    assert uuid.UUID(hex=p.script_id).version == 4
    json.dumps(p.segments, ensure_ascii=False)  # listo para JSON


def parse(text: str) -> ParsedScript:
    p = parse_script(text)
    check_invariants(p)
    return p


_REF_BREAKS = {"\x0b": "\n", "\x0c": "\n", "\x85": "\n", " ": "\n", " ": "\n\n"}
_REF_INVISIBLE = frozenset("­​‎‏⁠﻿")


def reference_text(text: str) -> str:
    """Normalización del texto según las reglas, carácter a carácter."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for c in text:
        if c in _REF_BREAKS:
            out.append(_REF_BREAKS[c])
        elif c in _REF_INVISIBLE:
            continue
        elif 0xD800 <= ord(c) <= 0xDFFF:
            out.append("�")
        else:
            out.append(c)
    return "".join(out)


def reference_shape(text: str) -> list:
    """Implementación ingenua de las reglas, para contrastar con el parser.

    Cada `[` busca su cierre contando anidamiento hacia adelante; si no lo
    encuentra, es texto.
    """
    text = reference_text(text)
    items: list[tuple] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            items.append(("w", "".join(buf)))
            buf.clear()

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "[":
            depth, j = 0, i
            while j < n:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n:
                flush()
                items.append(("d", text[i:j + 1]))
                i = j + 1
                continue
        if c == "\n":
            flush()
            items.append(("nl",))
        elif c.isspace():
            flush()
        else:
            buf.append(c)
        i += 1
    flush()

    out: list = []
    newlines, prev, count = 0, None, 0
    for it in items:
        if it[0] == "nl":
            newlines += 1
            continue
        is_dir = it[0] == "d"
        if newlines and prev is not None:
            kind = "paragraph" if newlines >= 2 else "line"
            if kind == "paragraph" or (prev == "w" and not is_dir):
                out.append(("break", kind))
        newlines = 0
        if is_dir:
            out.append(d(" ".join(it[1].split()), count))
        else:
            out.append(it[1])
            count += 1
        prev = it[0]

    # at_idx: la primera palabra con unidades desde la posición de la directiva.
    readable = [bool(to_units(x)) for x in out if isinstance(x, str)]

    def first_readable(i: int) -> int:
        while i < len(readable) and not readable[i]:
            i += 1
        return i

    return [d(x[1], first_readable(x[2])) if isinstance(x, tuple) and x[0] == "dir" else x
            for x in out]


# ---------------------------------------------------------------- hallazgos C1 y A1

def test_c1_tokens_sin_unidades_conservan_indice() -> None:
    text = f"Hola {EM_DASH} mundo. Esto es la RTX-5070 ... y el sistema ¿funciona ? sí"
    p = parse(text)
    assert p.total_tokens == 14
    assert [t.text for t in p.tokens] == text.split()
    skipped = [s["idx"] for s in word_segments(p) if s.get("skip")]
    assert skipped == [1, 7, 12]
    assert [p.tokens[i].text for i in skipped] == [EM_DASH, "...", "?"]
    assert p.tokens[6].units == ["rtx", "cinco", "mil", "setenta"]
    assert p.tokens[11].units == ["funciona"]
    assert p.tokens[13].units == ["si"]


def test_a1_directivas_consecutivas_al_inicio_comparten_at_idx() -> None:
    p = parse("[Cambiar a slide 2] [Mostrar grafico] Hola mundo")
    assert shape(p) == [
        d("[Cambiar a slide 2]", 0), d("[Mostrar grafico]", 0), "Hola", "mundo",
    ]


def test_directivas_consecutivas_en_medio_comparten_at_idx() -> None:
    p = parse("Hola [a] [b]\n[c] mundo")
    assert shape(p) == ["Hola", d("[a]", 1), d("[b]", 1), d("[c]", 1), "mundo"]


# ---------------------------------------------------------------- directivas

def test_directiva_al_inicio() -> None:
    p = parse("[Intro] Buenas tardes")
    assert shape(p) == [d("[Intro]", 0), "Buenas", "tardes"]


def test_directiva_al_final_tiene_at_idx_total() -> None:
    p = parse("Muchas gracias. [Fin]")
    assert shape(p) == ["Muchas", "gracias.", d("[Fin]", 2)]
    assert p.segments[-1]["at_idx"] == p.total_tokens == 2


def test_directivas_consecutivas_al_final() -> None:
    p = parse("Gracias\n\n[Aplausos]\n[Fin]\n")
    assert shape(p) == ["Gracias", PARA, d("[Aplausos]", 1), d("[Fin]", 1)]


def test_solo_directivas() -> None:
    p = parse("[Uno] [Dos]")
    assert p.total_tokens == 0
    assert shape(p) == [d("[Uno]", 0), d("[Dos]", 0)]


def test_corchetes_anidados_forman_una_sola_directiva() -> None:
    p = parse("Antes [a [b] c] después")
    assert shape(p) == ["Antes", d("[a [b] c]", 1), "después"]
    p = parse("[a [b [c]] d][e]")
    assert shape(p) == [d("[a [b [c]] d]", 0), d("[e]", 0)]


def test_corchete_sin_cierre_es_texto() -> None:
    p = parse("Hola [sin cierre mundo")
    assert shape(p) == ["Hola", "[sin", "cierre", "mundo"]
    assert p.tokens[1].units == ["sin"]
    p = parse("Hola [")
    assert shape(p) == ["Hola", "["]
    assert word_segments(p)[1].get("skip") is True


def test_corchete_sin_cierre_con_directiva_interior() -> None:
    p = parse("Texto [abierto y [dir] fin")
    assert shape(p) == ["Texto", "[abierto", "y", d("[dir]", 3), "fin"]


def test_corchete_de_cierre_suelto_es_texto() -> None:
    p = parse("Hola ] mundo")
    assert shape(p) == ["Hola", "]", "mundo"]
    assert word_segments(p)[1].get("skip") is True
    p = parse("fin] [Pausa] sigue")
    assert shape(p) == ["fin]", d("[Pausa]", 1), "sigue"]
    assert p.tokens[0].units == ["fin"]
    p = parse("]] [x]")
    assert shape(p) == ["]]", d("[x]", 1)]


def test_directiva_pegada_a_palabras() -> None:
    p = parse("Hola[pausa]mundo")
    assert shape(p) == ["Hola", d("[pausa]", 1), "mundo"]


def test_directiva_multilinea_colapsa_espacios() -> None:
    p = parse("Hola [Mostrar\n   slide\r\n\r\n\t2] mundo")
    assert shape(p) == ["Hola", d("[Mostrar slide 2]", 1), "mundo"]


def test_directiva_colapsa_espacios_repetidos() -> None:
    p = parse("[  Mostrar    gráfico  ]")
    assert shape(p) == [d("[ Mostrar gráfico ]", 0)]


def test_directiva_vacia() -> None:
    assert shape(parse("a [] b")) == ["a", d("[]", 1), "b"]


# ---------------------------------------------------------------- espacios y saltos

def test_crlf_se_normaliza() -> None:
    p = parse("Hola\r\nmundo\r\n\r\nfin")
    assert p.text == "Hola\nmundo\n\nfin"
    assert shape(p) == ["Hola", LINE, "mundo", PARA, "fin"]


def test_cr_suelto_se_normaliza() -> None:
    p = parse("Hola\rmundo\r\rfin")
    assert p.text == "Hola\nmundo\n\nfin"
    assert shape(p) == ["Hola", LINE, "mundo", PARA, "fin"]


def test_crlf_y_cr_mezclados() -> None:
    p = parse("a\r\n\rb")
    assert p.text == "a\n\nb"
    assert shape(p) == ["a", PARA, "b"]


def test_tabs_cuentan_como_espacio() -> None:
    p = parse("Hola\tmundo\t\t fin")
    assert shape(p) == ["Hola", "mundo", "fin"]
    assert p.text == "Hola\tmundo\t\t fin"


def test_espacios_multiples() -> None:
    assert shape(parse("  Hola     mundo   ")) == ["Hola", "mundo"]


def test_emojis_son_tokens_sin_unidades() -> None:
    p = parse(f"Hola {ROCKET} mundo {WAVE_SKIN} Genial{ROCKET}")
    assert shape(p) == ["Hola", ROCKET, "mundo", WAVE_SKIN, f"Genial{ROCKET}"]
    skipped = [s["idx"] for s in word_segments(p) if s.get("skip")]
    assert skipped == [1, 3]
    assert p.tokens[4].units == ["genial"]


def test_salto_simple_es_line() -> None:
    assert shape(parse("Hola\nmundo")) == ["Hola", LINE, "mundo"]


def test_linea_en_blanco_es_paragraph() -> None:
    assert shape(parse("Hola\n\nmundo")) == ["Hola", PARA, "mundo"]
    assert shape(parse("Hola\n \t \nmundo")) == ["Hola", PARA, "mundo"]


def test_nunca_dos_break_seguidos() -> None:
    assert shape(parse("a\n\n\n\n\nb")) == ["a", PARA, "b"]
    assert shape(parse("a\n \n\t\n  \nb\nc")) == ["a", PARA, "b", LINE, "c"]


def test_sin_break_al_principio_ni_al_final() -> None:
    assert shape(parse("\n\n  Hola mundo\n\n  ")) == ["Hola", "mundo"]
    assert shape(parse("\n[d]\n\n")) == [d("[d]", 0)]


@pytest.mark.parametrize("text,expected", [
    ("a\n[d]\nb", ["a", d("[d]", 1), "b"]),
    ("a\n\n[d]\n\nb", ["a", PARA, d("[d]", 1), PARA, "b"]),
    ("a\n\n[d]\nb", ["a", PARA, d("[d]", 1), "b"]),
    ("a\n[d]\n\nb", ["a", d("[d]", 1), PARA, "b"]),
    ("[d]\nb", [d("[d]", 0), "b"]),
    ("a\n[d]", ["a", d("[d]", 1)]),
    ("[a]\n[b]", [d("[a]", 0), d("[b]", 0)]),
    ("[a]\n\n[b]", [d("[a]", 0), PARA, d("[b]", 0)]),
    ("a\n[d] b\nc", ["a", d("[d]", 1), "b", LINE, "c"]),
])
def test_junto_a_directivas_solo_se_conservan_paragraph(text: str, expected: list) -> None:
    assert shape(parse(text)) == expected


def test_bom_inicial_se_descarta() -> None:
    p = parse("\ufeffHola mundo")
    assert p.text == "Hola mundo"
    assert shape(p) == ["Hola", "mundo"]


# ---------------------------------------------------------------- separadores y caracteres invisibles

@pytest.mark.parametrize("sep", ["\x0b", "\x0c", "\x85", "\u2028"])
def test_otros_separadores_de_linea_son_line(sep: str) -> None:
    # VT es el salto de May\u00fas+Intro de Word y PowerPoint (notas del orador).
    p = parse(f"Hola{sep}mundo")
    assert p.text == "Hola\nmundo"
    assert shape(p) == ["Hola", LINE, "mundo"]


def test_separador_de_parrafo_unicode_es_paragraph() -> None:
    p = parse("Hola\u2029mundo\x0b\x0bfin")
    assert shape(p) == ["Hola", PARA, "mundo", PARA, "fin"]


def test_separadores_mezclados_conservan_la_estructura() -> None:
    p = parse("Hola\u2028mundo\u2029fin\x0bya\x0cok\x85x")
    assert shape(p) == ["Hola", LINE, "mundo", PARA, "fin", LINE, "ya", LINE, "ok", LINE, "x"]


def test_guion_blando_no_parte_la_palabra() -> None:
    p = parse("Explico el funcio\u00adnamiento del sistema")
    assert p.tokens[2].text == "funcionamiento"
    assert p.tokens[2].units == ["funcionamiento"]
    assert p.text == "Explico el funcionamiento del sistema"


@pytest.mark.parametrize("invisible", ["\u200b", "\u2060", "\ufeff", "\u200e", "\u200f"])
def test_caracteres_de_formato_invisibles_se_quitan(invisible: str) -> None:
    p = parse(f"pala{invisible}bra")
    assert [t.text for t in p.tokens] == ["palabra"]
    # Una l\u00ednea con solo el car\u00e1cter invisible es una l\u00ednea en blanco.
    p = parse(f"Hola\n{invisible}\nmundo")
    assert shape(p) == ["Hola", PARA, "mundo"]


def test_zwj_de_los_emojis_se_conserva() -> None:
    familia = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    p = parse(f"Hola {familia} mundo")
    assert p.tokens[1].text == familia and p.tokens[1].units == []


def test_sustituto_suelto_se_cambia_por_el_caracter_de_reemplazo() -> None:
    # JSON.stringify de un emoji partido llega al backend como "\ud83d" suelto:
    # no se puede codificar en UTF-8 y el env\u00edo de script_loaded fallar\u00eda.
    p = parse("Hola \ud83d mundo \udc00fin")
    assert p.text == "Hola \ufffd mundo \ufffdfin"
    assert [t.text for t in p.tokens] == ["Hola", "\ufffd", "mundo", "\ufffdfin"]
    assert p.tokens[1].units == [] and p.tokens[3].units == ["fin"]
    json.dumps(p.segments, ensure_ascii=False).encode("utf-8")


def test_emoji_completo_no_se_toca() -> None:
    p = parse(f"Hola {ROCKET}")
    assert p.text == f"Hola {ROCKET}"


# ---------------------------------------------------------------- at_idx y tokens sin unidades

def test_directiva_seguida_de_guion_largo_apunta_a_la_palabra() -> None:
    p = parse(f"[Entra Juan]\n{EM_DASH} Hola, \u00bfqu\u00e9 tal?")
    assert shape(p) == [d("[Entra Juan]", 1), EM_DASH, "Hola,", "\u00bfqu\u00e9", "tal?"]


def test_directivas_en_dialogo_quedan_actuales() -> None:
    # Hallazgo: con at_idx en el guion largo o en "...", que el matcher salta,
    # la directiva pasaba de "pr\u00f3xima" a "le\u00edda" sin estar nunca "actual".
    text = (f"Buenos d\u00edas a todos.\n\n[Entra Juan]\n{EM_DASH} Hola, \u00bfqu\u00e9 tal?\n\n"
            "[Mostrar slide 2] ... y ahora los datos.")
    p = parse(text)
    dirs = [s for s in p.segments if s["type"] == "directive"]
    assert [p.tokens[s["at_idx"]].text for s in dirs] == ["Hola,", "y"]
    m = ScriptMatcher(p.tokens)
    seen = {s["text"]: set() for s in dirs}
    # Ventanas solapadas, como en vivo (matcher v2: cada ciclo repite la cola anterior).
    for spoken in ("buenos dias", "buenos dias a todos", "dias a todos hola que",
                   "a todos hola que tal", "hola que tal y ahora", "que tal y ahora los datos"):
        cur = m.update(to_units(spoken)).new_idx
        for s in dirs:
            state = "read" if cur > s["at_idx"] else "current" if cur == s["at_idx"] else "upcoming"
            seen[s["text"]].add(state)
    assert all("current" in states for states in seen.values()), seen


def test_directiva_seguida_solo_de_signos_apunta_al_total() -> None:
    p = parse(f"Gracias [Fin] {EM_DASH} ...")
    assert shape(p) == ["Gracias", d("[Fin]", 3), EM_DASH, "..."]
    assert p.total_tokens == 3


def test_directivas_consecutivas_ante_signos_comparten_at_idx() -> None:
    p = parse(f"[a] [b] {EM_DASH} \u00bf hola")
    assert shape(p) == [d("[a]", 2), d("[b]", 2), EM_DASH, "\u00bf", "hola"]


# ---------------------------------------------------------------- vacío y límites

@pytest.mark.parametrize("text", ["", " ", "   \t ", "\n\n", "\r\n\t \r", "\ufeff"])
def test_texto_vacio_o_solo_espacios(text: str) -> None:
    p = parse(text)
    assert p.total_tokens == 0
    assert p.tokens == []
    assert p.segments == []


def test_texto_mayor_que_el_maximo_lanza_value_error() -> None:
    with pytest.raises(ValueError):
        parse_script("a" * (MAX_SCRIPT_CHARS + 1))


def test_texto_del_tamano_maximo_se_acepta() -> None:
    p = parse_script("x" * MAX_SCRIPT_CHARS)
    assert p.total_tokens == 1


def test_texto_que_no_es_str_lanza_type_error() -> None:
    with pytest.raises(TypeError):
        parse_script(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_script(b"Hola")  # type: ignore[arg-type]


def test_script_id_nuevo_en_cada_parseo() -> None:
    a = parse("Hola")
    b = parse("Hola")
    assert a.script_id != b.script_id


def test_tokens_con_el_mismo_texto_no_comparten_lista_de_unidades() -> None:
    p = parse("hola hola")
    assert p.tokens[0].units == p.tokens[1].units == ["hola"]
    assert p.tokens[0].units is not p.tokens[1].units


# ---------------------------------------------------------------- guion de ejemplo

def test_guion_de_ejemplo() -> None:
    raw = EJEMPLO.read_text(encoding="utf-8")
    p = parse(raw)
    assert p.total_tokens == len(raw.split()) == 89
    assert [t.text for t in p.tokens] == raw.split()
    breaks = [s["kind"] for s in p.segments if s["type"] == "break"]
    assert breaks == ["paragraph"] * 4  # 5 párrafos
    assert not [s for s in p.segments if s["type"] == "directive"]
    assert all(t.units for t in p.tokens)


def test_unidades_del_guion_coinciden_con_la_transcripcion() -> None:
    # Dígitos en el guion y palabras en la transcripción.
    p = parse("Con la RTX-5070 ti, en 2026.")
    flat = [u for t in p.tokens for u in t.units]
    assert flat == to_units("con la rtx cinco mil setenta ti en dos mil veintiséis")
    # Palabras en el guion y dígitos en la transcripción.
    raw = EJEMPLO.read_text(encoding="utf-8")
    p = parse(raw)
    flat = [u for t in p.tokens for u in t.units]
    frase = to_units("la tarjeta gráfica RTX 5070 Ti.")
    assert any(flat[i:i + len(frase)] == frase for i in range(len(flat)))


# ---------------------------------------------------------------- propiedades

FUZZ_PIECES = [
    "a", "b", "Ñu", "Á", "5", "2026", "x-1", " ", "  ", "\t", "\n", "\r", "\r\n",
    "[", "]", "[", "]", EM_DASH, "...", "?", ROCKET, "\u00a0",
    "\x0b", "\x0c", "\x85", "\u2028", "\u2029", "\u00ad", "\u200b", "\u2060", "\ufeff",
    "\ud83d",  # sustituto suelto (emoji partido)
]


def test_invariantes_y_referencia_con_textos_aleatorios() -> None:
    rnd = random.Random(20261009)
    for _ in range(400):
        text = "".join(rnd.choice(FUZZ_PIECES) for _ in range(rnd.randint(0, 60)))
        p = parse(text)
        assert shape(p) == reference_shape(text), repr(text)


def test_parsea_100k_palabras_en_menos_de_1_s() -> None:
    vocab = EJEMPLO.read_text(encoding="utf-8").split()
    rnd = random.Random(7)
    parts: list[str] = []
    for i in range(100_000):
        # Una de cada diez es única y lleva número: obliga a normalizar de verdad.
        parts.append(f"término{i}" if i % 10 == 0 else rnd.choice(vocab))
        if i % 500 == 499:
            parts.append("\n\n[Cambiar de slide]\n\n")
        elif i % 50 == 49:
            parts.append("\n\n")
        elif i % 12 == 11:
            parts.append("\n")
        else:
            parts.append(" ")
    text = "".join(parts)

    t0 = time.perf_counter()
    p = parse_script(text)
    elapsed = time.perf_counter() - t0

    assert p.total_tokens == 100_000
    assert p.tokens[-1].idx == 99_999
    assert elapsed < 1.0, f"parse_script tardó {elapsed:.3f} s"
