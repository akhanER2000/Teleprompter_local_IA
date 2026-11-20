"""Parser del guion: segmentos (word, directive, ref, break) y tokens con unidades.

El backend es la única fuente de verdad del guion: el frontend dibuja los
`segments` 1:1 y el matcher trabaja con `tokens`, que comparten el índice.

Hito 85 (R2): una directiva con prefijo `img:`, `ppt:`, `pdf:`, `code:` o
`nota:` es una REFERENCIA (`type: "ref"`). No da tokens, así que la voz y el
matcher la ignoran igual que a una directiva; tampoco es sección.
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


# Hito 85 (R2): `[tipo: archivo#fragmento]`. El prefijo no distingue mayúsculas.
REF_KINDS = ("img", "ppt", "pdf", "code", "nota")
_REF_RE = re.compile(r"^\[\s*(img|ppt|pdf|code|nota)\s*:\s*(.*?)\s*\]$", re.IGNORECASE | re.DOTALL)
_PAGE_RE = re.compile(r"^\d{1,5}$")
_LINES_RE = re.compile(r"^L?(\d{1,6})(?:\s*-\s*L?(\d{1,6}))?$", re.IGNORECASE)
NOTE_MAX_CHARS = 400
LABEL_MAX_CHARS = 60


def parse_ref(directive_text: str) -> dict | None:
    """Campos de una referencia si `directive_text` (con corchetes) lo es; si no, None.

    `file` es el nombre tal como se escribió y `frag` lo que va tras el último
    `#` (o None). `page` (ppt, pdf: 1 si no se indica) y `lines` (code: [desde,
    hasta] o None = archivo entero) salen del fragmento; un fragmento que no se
    entiende deja `page`/`lines` en None y `bad_frag` en True: el servidor
    responde «No encontrado» y la lectura sigue.
    """
    m = _REF_RE.match(directive_text)
    if m is None:
        return None
    kind, body = m.group(1).lower(), m.group(2).strip()
    if not body:
        return None   # `[img:]` sin nada: queda como directiva
    if kind == "nota":
        note = body[:NOTE_MAX_CHARS]
        short = note if len(note) <= LABEL_MAX_CHARS else note[:LABEL_MAX_CHARS - 1] + "…"
        return {"kind": "nota", "file": None, "frag": None, "note": note,
                "label": f"nota: {short}"}
    file, frag = body, None
    if kind != "img" and "#" in body:
        file, frag = (x.strip() for x in body.rsplit("#", 1))
        frag = frag or None
    if not file:
        return None
    ref: dict = {"kind": kind, "file": file, "frag": frag}
    if kind in ("ppt", "pdf"):
        if frag is None:
            ref["page"] = 1
        elif _PAGE_RE.match(frag) and int(frag) >= 1:
            ref["page"] = int(frag)
        else:
            ref["page"], ref["bad_frag"] = None, True
    elif kind == "code":
        lm = _LINES_RE.match(frag) if frag is not None else None
        if frag is None:
            ref["lines"] = None
        elif lm is None or int(lm.group(1)) < 1 or int(lm.group(2) or lm.group(1)) < int(lm.group(1)):
            ref["lines"], ref["bad_frag"] = None, True
        else:
            ref["lines"] = [int(lm.group(1)), int(lm.group(2) or lm.group(1))]
    name = file.replace("\\", "/").rsplit("/", 1)[-1] or file
    ref["label"] = f"{kind}: {name}"[:LABEL_MAX_CHARS + 6]
    return ref


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
        if seg["type"] not in ("directive", "ref"):
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
    # Una referencia sola en su renglón se lleva un salto: el texto queda como
    # si ese renglón no estuviera: "A", "[img: x]", "B" en tres renglones se
    # dibuja como "A" y "B" en dos.
    swallow = False

    end_of_text = len(text)
    pos = 0
    # El span final vacío es un centinela para procesar el último tramo.
    for start, end in [*_directive_spans(text), (end_of_text, end_of_text)]:
        for m in _ITEM_RE.finditer(text, pos, start):
            word = m.group()
            if word == "\n":
                if swallow:
                    swallow = False
                else:
                    newlines += 1
                continue
            swallow = False
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
        raw = _SPACE_RE.sub(" ", text[start:end])
        ref = parse_ref(raw)
        if ref is not None:
            # La referencia no cierra el renglón ni cambia `prev`: los saltos
            # pendientes se resuelven en la palabra siguiente.
            swallow = newlines > 0 or prev is None
            segments.append({"type": "ref", **ref, "text": raw, "at_idx": len(tokens)})
            pos = end
            continue
        if newlines:
            if prev is not None and newlines > 1:
                segments.append({"type": "break", "kind": "paragraph"})
            newlines = 0
        swallow = False
        segments.append({
            "type": "directive",
            "text": raw,
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
