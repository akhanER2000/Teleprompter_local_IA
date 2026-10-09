"""Parser del guion: segmentos (word, directive, break) y tokens con unidades.

El backend es la única fuente de verdad del guion: el frontend dibuja los
`segments` 1:1 y el matcher trabaja con `tokens`, que comparten el índice.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from tools.text_norm import to_units

MAX_SCRIPT_CHARS = 2_000_000

_BRACKET_RE = re.compile(r"[\[\]]")
# Un salto de línea o una palabra (corrida sin espacio en blanco).
_ITEM_RE = re.compile(r"\n|\S+")
_SPACE_RE = re.compile(r"\s+")
# Otros separadores de línea y de párrafo que `\s` trataría como un espacio:
# VT es el salto de Mayús+Intro de Word y PowerPoint (notas del orador);
# FF, NEL y U+2028 aparecen al copiar de algunos editores.
_LINE_BREAKS = str.maketrans({
    "\x0b": "\n", "\x0c": "\n", "\x85": "\n", " ": "\n", " ": "\n\n",
})
# Caracteres de formato invisibles que partirían una palabra en unidades que
# nunca coinciden ("funcio<guion blando>namiento") o crearían tokens vacíos:
# guion blando, espacio de ancho cero, marcas de dirección, unión de palabra
# y BOM. El ZWJ (U+200D) se conserva: une secuencias de emoji.
_INVISIBLE_RE = re.compile("[­​‎‏⁠﻿]")
# Tras decodificar JSON, un code point sustituto en un str siempre está suelto
# (un emoji partido al pegar): no se puede codificar en UTF-8.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


@dataclass
class Token:
    idx: int
    text: str          # tal como se muestra (con puntuación)
    units: list[str]   # vacía = token que el matcher salta


@dataclass
class ParsedScript:
    script_id: str             # uuid4 en hex
    text: str                  # texto original normalizado a LF
    segments: list[dict]       # listo para JSON (viaja en script_loaded)
    tokens: list[Token]        # uno por segmento word, en orden, idx == posición
    total_tokens: int


def _directive_spans(text: str) -> list[tuple[int, int]]:
    """Spans `[inicio, fin)` de las directivas de primer nivel, en orden.

    Empareja corchetes con una pila, así `[a [b] c]` es una sola directiva.
    Un `[` sin cierre y un `]` suelto no forman directiva: quedan como texto.
    """
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for m in _BRACKET_RE.finditer(text):
        pos = m.start()
        if text[pos] == "[":
            stack.append(pos)
        elif stack:
            pairs[stack.pop()] = pos
    spans: list[tuple[int, int]] = []
    last_end = -1
    for start in sorted(pairs):
        if start > last_end:  # no está dentro de otra directiva
            last_end = pairs[start]
            spans.append((start, last_end + 1))
    return spans


def _normalize_text(text: str) -> str:
    """Texto que se tokeniza, se guarda y viaja en `script_loaded`.

    Saltos a LF (CRLF, CR, VT, FF, NEL y U+2028 dan un salto; U+2029, uno de
    párrafo), sin caracteres de formato invisibles y con cada sustituto suelto
    cambiado por U+FFFD, así el texto siempre se puede enviar como UTF-8.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_LINE_BREAKS)
    if not text.isascii():
        text = _INVISIBLE_RE.sub("", text)
        text = _SURROGATE_RE.sub("�", text)
    return text


def _point_directives_to_readable_words(segments: list[dict], tokens: list[Token]) -> None:
    """Ajusta `at_idx` de cada directiva a la primera palabra con unidades.

    Matcher, Auto-scroll y flechas saltan los tokens sin unidades, así que la
    posición nunca se detiene en un guion largo o en "..." tras una directiva:
    si `at_idx` apuntara ahí, la directiva no llegaría a estar "actual".
    """
    total = len(tokens)
    next_readable: list[int] | None = None
    for seg in segments:
        if seg["type"] != "directive":
            continue
        at = seg["at_idx"]
        if at >= total or tokens[at].units:
            continue
        if next_readable is None:  # una sola pasada hacia atrás: O(n) en total
            next_readable = [total] * (total + 1)
            for i in range(total - 1, -1, -1):
                next_readable[i] = i if tokens[i].units else next_readable[i + 1]
        seg["at_idx"] = next_readable[at]


def parse_script(text: str) -> ParsedScript:
    """Convierte el texto del guion en segmentos y tokens.

    Reglas en `docs/diseno_v2.md`, sección 4. Lanza `ValueError` si el texto
    supera `MAX_SCRIPT_CHARS`.
    """
    if not isinstance(text, str):
        raise TypeError("el guion debe ser texto (str)")
    if len(text) > MAX_SCRIPT_CHARS:
        raise ValueError(
            f"el guion tiene {len(text)} caracteres y el máximo es {MAX_SCRIPT_CHARS}"
        )
    text = _normalize_text(text)

    segments: list[dict] = []
    tokens: list[Token] = []
    memo: dict[str, list[str]] = {}  # las palabras se repiten mucho en un guion
    newlines = 0  # saltos de línea desde el último segmento de contenido
    prev: str | None = None  # tipo del último segmento de contenido

    end_of_text = len(text)
    pos = 0
    # El span final vacío es un centinela para procesar el último tramo.
    for start, end in [*_directive_spans(text), (end_of_text, end_of_text)]:
        for m in _ITEM_RE.finditer(text, pos, start):
            word = m.group()
            if word == "\n":
                newlines += 1
                continue
            if newlines:
                # Un salto simple junto a una directiva no se conserva.
                if prev is not None and (newlines > 1 or prev == "word"):
                    kind = "paragraph" if newlines > 1 else "line"
                    segments.append({"type": "break", "kind": kind})
                newlines = 0
            units = memo.get(word)
            if units is None:
                units = memo[word] = to_units(word)
            idx = len(tokens)
            tokens.append(Token(idx, word, list(units)))
            if units:
                segments.append({"type": "word", "idx": idx, "text": word})
            else:
                segments.append({"type": "word", "idx": idx, "text": word, "skip": True})
            prev = "word"
        if start == end_of_text:
            break
        if newlines:
            if prev is not None and newlines > 1:
                segments.append({"type": "break", "kind": "paragraph"})
            newlines = 0
        segments.append({
            "type": "directive",
            "text": _SPACE_RE.sub(" ", text[start:end]),
            "at_idx": len(tokens),  # se ajusta abajo a la primera palabra con unidades
        })
        prev = "directive"
        pos = end

    _point_directives_to_readable_words(segments, tokens)
    return ParsedScript(
        script_id=uuid.uuid4().hex,
        text=text,
        segments=segments,
        tokens=tokens,
        total_tokens=len(tokens),
    )
