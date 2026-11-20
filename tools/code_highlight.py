"""Hito 85 (R4): un trozo de código coloreado como HTML, con Pygments (local, sin CDN).

Devuelve solo `<span class="c-…">`: los colores están en frontend/tema.css
(`.ref-codigo .c-k`, …). Pygments escapa el texto, así que el HTML es seguro.
"""
from __future__ import annotations

from pathlib import Path

MAX_BYTES = 1_000_000   # un archivo de código más grande no es un trozo
MAX_LINES = 80          # lo que cabe en una tarjeta ampliada


class CodeError(LookupError):
    """No es un archivo de texto legible o las líneas no existen."""


def highlight(src: Path, lines: list[int] | None) -> dict:
    """{"html", "desde", "hasta", "total", "lenguaje"}. `lines` = [desde, hasta] (desde 1) o None."""
    from pygments import highlight as _hl
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import TextLexer, get_lexer_for_filename
    from pygments.util import ClassNotFound

    try:
        raw = src.read_bytes()[:MAX_BYTES + 1]
    except OSError as exc:
        raise CodeError(str(exc)) from exc
    if len(raw) > MAX_BYTES or b"\x00" in raw:
        raise CodeError("no es un archivo de texto (o supera 1 MB)")
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    all_lines = text.split("\n")
    if all_lines and all_lines[-1] == "":
        all_lines.pop()
    total = len(all_lines)
    if lines is None:
        a, b = 1, min(total, MAX_LINES)
    else:
        a, b = lines
        if a > total:
            raise CodeError(f"el archivo tiene {total} líneas y se pidió la {a}")
        b = min(b, total, a + MAX_LINES - 1)
    chunk = "\n".join(all_lines[a - 1:b]) + "\n"
    try:
        lexer = get_lexer_for_filename(src.name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        lexer = TextLexer(stripnl=False, ensurenl=False)
    html = _hl(chunk, lexer, HtmlFormatter(nowrap=True, classprefix="c-"))
    return {"html": html, "desde": a, "hasta": b, "total": total, "lenguaje": lexer.name}
