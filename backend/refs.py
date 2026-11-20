"""Hito 85 (R2-R4): sirve las referencias del guion por una ruta local.

    GET /api/ref?tipo=img|ppt|pdf|code&archivo=<nombre>&guion=<guion>&pagina=N&desde=A&hasta=B

- img, pdf y ppt: un PNG cacheado en `guiones/.cache/` (tools/ref_thumbs.py).
- code: JSON con el HTML coloreado (tools/code_highlight.py).
- Cualquier fallo: 404 con `{"detail": "No encontrado: <nombre>"}`; la tarjeta lo
  muestra y la lectura sigue. Sin PowerPoint ni LibreOffice: 501 con
  `{"code": "SIN_MOTOR"}`, y la tarjeta pide exportar la diapositiva como imagen.
- Solo para la propia página: una petición de otro sitio (Sec-Fetch-Site:
  cross-site) se rechaza con 403.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

from tools import code_highlight, ref_thumbs, slides_engine
from tools.ref_resolve import resolve_ref

log = logging.getLogger("teleprompter.refs")

KINDS = ("img", "ppt", "pdf", "code")
CACHE_DIRNAME = ".cache"


def _name(file: str, extra: str = "") -> str:
    base = file.replace("\\", "/").rsplit("/", 1)[-1] or file
    return f"{base}{extra}"


def not_found(file: str, extra: str = "", why: str = "") -> JSONResponse:
    if why:
        log.info("Referencia no encontrada: %s%s (%s)", file, extra, why)
    return JSONResponse({"detail": f"No encontrado: {_name(file, extra)}", "code": "NO_ENCONTRADO"},
                        status_code=404)


async def serve_ref(scripts_dir: Path, *, tipo: str, archivo: str, guion: str | None,
                    pagina: int | None, desde: int | None, hasta: int | None,
                    fetch_site: str | None):
    if fetch_site == "cross-site":
        return JSONResponse({"detail": "Solo para la página del teleprompter."}, status_code=403)
    if tipo not in KINDS or not archivo or len(archivo) > 1024:
        return not_found(archivo or "?", why="petición inválida")
    page = 1 if pagina is None else pagina
    extra = f"#{page}" if tipo in ("ppt", "pdf") and pagina is not None else ""
    if tipo == "code" and desde is not None:
        extra = f"#L{desde}-L{hasta if hasta is not None else desde}"
    if page < 1 or (desde is not None and desde < 1) or (hasta is not None and desde is not None and hasta < desde):
        return not_found(archivo, extra, "fragmento inválido")

    src = await asyncio.to_thread(resolve_ref, scripts_dir, guion, tipo, archivo)
    if src is None:
        return not_found(archivo, extra, "no existe en _recursos/, en guiones/ ni como ruta absoluta")

    if tipo == "code":
        lines = None if desde is None else [desde, hasta if hasta is not None else desde]
        try:
            data = await asyncio.to_thread(code_highlight.highlight, src, lines)
        except code_highlight.CodeError as exc:
            return not_found(archivo, extra, str(exc))
        return JSONResponse(data, headers={"Cache-Control": "no-cache"})

    try:
        out, fresh = await asyncio.to_thread(ref_thumbs.thumbnail, tipo, src,
                                             scripts_dir / CACHE_DIRNAME, page)
    except slides_engine.NoEngine:
        return JSONResponse({"detail": f"Exporta la diapositiva como imagen: {_name(archivo, extra)}",
                             "code": "SIN_MOTOR"}, status_code=501)
    except (ref_thumbs.RefNotFound, slides_engine.ConvertError) as exc:
        return not_found(archivo, extra, str(exc))
    except Exception as exc:   # ningún error silencioso: queda en el log y la tarjeta lo dice
        log.exception("No se pudo dibujar la referencia %s %s.", tipo, archivo)
        return not_found(archivo, extra, f"{type(exc).__name__}: {exc}")
    if fresh:
        log.info("Miniatura hecha: %s %s%s -> %s", tipo, archivo, extra, out.name)
    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "no-cache", "X-TP-Rehecha": "1" if fresh else "0"})
