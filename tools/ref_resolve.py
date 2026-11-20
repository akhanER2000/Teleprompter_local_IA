"""Hito 85 (R2): dónde está el archivo de una referencia del guion.

Orden de búsqueda: `guiones/<nombre del guion>_recursos/`, después junto al
guion (la carpeta `guiones/`) y por último la ruta absoluta tal como se
escribió. Solo se aceptan las extensiones de cada tipo; lo demás es «no
encontrado». No lee el archivo: solo dice cuál es.
"""
from __future__ import annotations

from pathlib import Path

EXTENSIONS = {
    "img": frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"}),
    "ppt": frozenset({".pptx", ".ppt", ".pps", ".ppsx", ".odp"}),
    "pdf": frozenset({".pdf"}),
    "code": None,   # cualquier archivo de texto (lo valida el colorizador)
}


def resources_dir(scripts_dir: Path, script_name: str | None) -> Path | None:
    """`guiones/<stem>_recursos/` del guion, o None si no se sabe qué guion es."""
    if not script_name:
        return None
    stem = Path(Path(script_name).name).stem
    if not stem or stem in (".", ".."):
        return None
    return scripts_dir / f"{stem}_recursos"


def candidates(scripts_dir: Path, script_name: str | None, file: str) -> list[Path]:
    """Rutas a probar, en el orden de R2."""
    out: list[Path] = []
    rel = Path(file)
    if not rel.is_absolute():
        res = resources_dir(scripts_dir, script_name)
        if res is not None:
            out.append(res / rel)
        out.append(scripts_dir / rel)
    else:
        out.append(rel)
    return out


def resolve_ref(scripts_dir: Path, script_name: str | None, kind: str, file: str) -> Path | None:
    """El primer candidato que existe y tiene una extensión válida para `kind`; si no, None."""
    if kind not in EXTENSIONS or not file or "\x00" in file:
        return None
    allowed = EXTENSIONS[kind]
    for path in candidates(scripts_dir, script_name, file):
        try:
            if allowed is not None and path.suffix.lower() not in allowed:
                continue
            if path.is_file():
                return path.resolve()
        except OSError:   # nombre inválido en Windows, sin permiso...
            continue
    return None
