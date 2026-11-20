"""Hito 85 (R4): miniaturas PNG de las referencias, cacheadas en `guiones/.cache/`.

- Imagen (png, jpg, webp, gif): Pillow; de un GIF, el primer cuadro.
- PDF: pypdfium2 (PDFium, local).
- Diapositiva: la presentación se convierte a PDF una vez (tools/slides_engine.py)
  y cada diapositiva se dibuja como una página.

La clave de la caché lleva la ruta, el `mtime_ns` y el tamaño del archivo (si
cambia el archivo, se rehace) y `THUMB_GEN`, que se sube a mano al cambiar el
algoritmo de dibujo: contar el contenido no detecta un cambio de algoritmo.
Nada de la caché se borra; una entrada vieja simplemente deja de pedirse.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from tools import slides_engine

THUMB_GEN = 1          # súbelo a mano si cambia cómo se dibuja una miniatura
THUMB_MAX_PX = 1280    # lado mayor: sirve para la tarjeta y para «Ampliar»

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_pdfium_lock = threading.Lock()   # PDFium no es seguro entre hilos


class RefNotFound(LookupError):
    """El archivo no existe, no se puede leer o la página no está."""


class PageOutOfRange(RefNotFound):
    pass


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def file_key(src: Path, *parts: object) -> str:
    st = src.stat()
    raw = "|".join(str(x) for x in (THUMB_GEN, src.resolve(), st.st_mtime_ns, st.st_size, *parts))
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _save_png(img, out: Path) -> None:
    from PIL import Image

    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.Resampling.LANCZOS)
    tmp = out.with_suffix(".tmp")
    img.save(tmp, format="PNG", optimize=True)
    tmp.replace(out)   # el archivo final aparece completo o no aparece


def _image(src: Path, out: Path) -> None:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(src) as im:
            im.seek(0)
            img = ImageOps.exif_transpose(im.copy())
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise RefNotFound(f"imagen ilegible: {exc}") from exc
    _save_png(img, out)


def _pdf_page(pdf: Path, page: int, out: Path) -> None:
    import pypdfium2 as pdfium

    with _pdfium_lock:
        try:
            doc = pdfium.PdfDocument(str(pdf))
        except (pdfium.PdfiumError, OSError) as exc:
            raise RefNotFound(f"PDF ilegible: {exc}") from exc
        try:
            n = len(doc)
            if not 1 <= page <= n:
                raise PageOutOfRange(f"el PDF tiene {n} páginas y se pidió la {page}")
            pg = doc[page - 1]
            w, h = pg.get_size()
            scale = THUMB_MAX_PX / max(w, h, 1)
            img = pg.render(scale=scale).to_pil()
            pg.close()
        finally:
            doc.close()
    _save_png(img, out)


def thumbnail(kind: str, src: Path, cache_dir: Path, page: int = 1) -> tuple[Path, bool]:
    """PNG de la referencia. Devuelve (ruta, rehecha). Lanza RefNotFound o slides_engine.*."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        key = file_key(src, kind, page, THUMB_MAX_PX)
    except OSError as exc:
        raise RefNotFound(str(exc)) from exc
    out = cache_dir / f"{key}.png"
    with _lock(key):
        if out.is_file():
            return out, False
        if kind == "img":
            _image(src, out)
        elif kind == "pdf":
            _pdf_page(src, page, out)
        elif kind == "ppt":
            _pdf_page(slides_pdf(src, cache_dir), page, out)
        else:
            raise ValueError(f"tipo sin miniatura: {kind!r}")
    return out, True


def slides_pdf(src: Path, cache_dir: Path) -> Path:
    """PDF de la presentación entera, convertido una vez por versión del archivo."""
    key = file_key(src, "slides")
    out_dir = cache_dir / f"{key}_pdf"
    pdf = out_dir / f"{src.stem}.pdf"
    with _lock(key):
        if not pdf.is_file():
            slides_engine.to_pdf(src, out_dir)
    return pdf
