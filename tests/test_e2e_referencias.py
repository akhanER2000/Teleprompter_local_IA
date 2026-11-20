"""Hito 85 de punta a punta: el visor real (Chrome headless con frontend/app.js) dibuja las
referencias del guion de ejemplo, y las órdenes nuevas de /control (view, ref_zoom) cambian
lo que se ve y los campos nuevos del estado (vista, ref_actual, ref_ampliada).

Mide la página, no los segmentos: qué tarjetas se ven, qué cargó cada una y qué dice una
referencia rota. Se salta (y lo dice) si no hay Chrome ni Edge.
"""
from __future__ import annotations

import asyncio

import pytest
import websockets

import e2e_cdp
from test_e2e_control import Ctl

pytestmark = pytest.mark.skipif(e2e_cdp.find_chrome() is None, reason="sin Chrome ni Edge")

ROTO = "Primero esto.\n[img: no_existe.png]\nY después lo otro, que sigue igual."


async def _scenario(port: int) -> None:
    async with e2e_cdp.chrome(port) as cdp:
        await e2e_cdp.open_app(cdp, port)
        await cdp.call("Page.reload")
        for _ in range(100):
            if await cdp.ev("!!window.TPDebug && document.getElementById('chip-conn').textContent === 'Servidor'"):
                break
            await asyncio.sleep(0.1)
        async with websockets.connect(f"ws://127.0.0.1:{port}/control") as ws:
            c = Ctl(ws)
            await c.until(lambda m: m["conectado"])
            refs: list = []

            async def check(cmd, value, pred, what):
                await c.send(cmd, value)
                try:
                    return await c.until(pred, timeout=8)
                except AssertionError as exc:
                    raise AssertionError(f"{cmd} {value}: {what} ({exc}); errores JS: {cdp.errors}") from None

            e = await check("open_script", "ejemplo_referencias", lambda m: m["guion"] == "ejemplo_referencias.txt",
                            "abrir el ejemplo")
            # Solo texto por defecto; las referencias no son secciones.
            assert e["vista"] == "texto" and e["secciones"] == 4 and e["ref_ampliada"] is False
            assert await cdp.ev("document.querySelectorAll('#script-view > .ref-card').length") == 5
            await cdp.ev("document.getElementById('btn-preview').click(); true")
            assert await cdp.ev("TPDebug.refs().filter(r => r.visible).length") == 0

            await check("view", "referencias", lambda m: m["vista"] == "referencias", "vista con referencias")
            for _ in range(200):
                refs = await cdp.ev("TPDebug.refs()")
                if all(r["img"] or r["codigo"] or r["kind"] == "nota" or r["error"] for r in refs):
                    break
                await asyncio.sleep(0.1)
            por_tipo = {r["kind"]: r for r in refs}
            assert all(r["visible"] for r in refs), refs
            assert por_tipo["img"]["img"] and por_tipo["pdf"]["img"] and por_tipo["code"]["codigo"], refs
            assert not por_tipo["nota"]["error"]
            # La diapositiva: imagen con LibreOffice/PowerPoint; sin motor, la tarjeta lo pide.
            ppt = por_tipo["ppt"]
            assert ppt["img"] or ppt["error"] == "Exporta la diapositiva como imagen: presentacion.pptx#2", ppt
            # Las tarjetas no se pisan entre sí.
            tops = [r["top"] for r in refs]
            assert tops == sorted(tops)

            # ref_actual: la que viene se anuncia antes de llegar.
            await check("top", None, lambda m: m["progreso"] == 0, "inicio")
            e = await c.until(lambda m: True)
            assert e["ref_actual"] is None   # «Buenas» está a más de 12 palabras de la portada
            await check("next_section", None, lambda m: m["seccion_idx"] == 1, "Resultados")
            e = await c.until(lambda m: m["ref_actual"] is not None)
            assert e["ref_actual"] == "ppt: presentacion.pptx"
            assert (await cdp.ev("TPDebug.refs()"))[1]["estado"] == "ref-proxima"

            # Ampliar y volver a pulsar.
            await check("ref_zoom", None, lambda m: m["ref_ampliada"] is True, "ampliar")
            assert await cdp.ev("!document.getElementById('ref-zoom').classList.contains('hidden')")
            assert "presentacion.pptx" in await cdp.ev("document.getElementById('ref-zoom-etiqueta').textContent")
            await check("ref_zoom", None, lambda m: m["ref_ampliada"] is False, "cerrar ampliar")
            # Tecla Z igual que la orden; V cambia de vista.
            await cdp.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'z', bubbles: true})); true")
            await c.until(lambda m: m["ref_ampliada"] is True)
            await cdp.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'z', bubbles: true})); true")
            await c.until(lambda m: m["ref_ampliada"] is False)
            await check("view", "siguiente", lambda m: m["vista"] == "texto", "siguiente: texto")
            await cdp.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'v', bubbles: true})); true")
            await c.until(lambda m: m["vista"] == "referencias")

            # La vista se recuerda entre sesiones (recarga de la página).
            await cdp.call("Page.reload")
            for _ in range(100):
                if await cdp.ev("!!window.TPDebug && document.body.dataset.vista === 'referencias'"):
                    break
                await asyncio.sleep(0.1)
            assert await cdp.ev("TPDebug.prefs().vista") == "referencias"

            # Un archivo que no existe: «No encontrado: nombre» y la lectura sigue.
            await e2e_cdp.load_script(cdp, ROTO)
            for _ in range(100):
                refs = await cdp.ev("TPDebug.refs()")
                if refs and refs[0]["error"]:
                    break
                await asyncio.sleep(0.1)
            assert refs[0]["error"] == "No encontrado: no_existe.png", refs
            color = await cdp.ev("getComputedStyle(document.querySelector('.ref-card.ref-error .ref-cuerpo')).color")
            assert color == "rgb(140, 135, 120)"   # --tp-grafito
            await check("play_pause", None, lambda m: m["corriendo"] is True, "lee igual")
            await asyncio.sleep(1.5)
            await check("play_pause", None, lambda m: m["corriendo"] is False and m["progreso"] > 0, "avanzó")
        assert not cdp.errors, cdp.errors
        assert not cdp.external(), cdp.external()


def test_vistas_referencias_y_ordenes_nuevas() -> None:
    port = e2e_cdp.free_port()
    with e2e_cdp.server(port):
        asyncio.run(_scenario(port))
