"""Hito 85: capturas de las dos vistas, en Chrome headless con el servidor real (STT falso).

    .venv312\\Scripts\\python.exe tools\\capturas_85.py docs\\hito85\\capturas

- Configuración y lectura en «Solo texto» y «Con referencias» a 400 y 1200 px de ancho.
- «Ampliar» (tecla Z) a 1200 px.
- «Sobre presentación» (fondo 0 %, referencias al 85 %): capturada con fondo TRANSPARENTE
  y compuesta sobre una diapositiva negra y una blanca (Pillow).
Mide en la página que ninguna tarjeta tape una palabra, que no haya scroll horizontal, que
cada tarjeta del guion de ejemplo haya cargado (imagen, diapositiva, PDF, código y nota), y
que no haya errores de JavaScript ni peticiones fuera de 127.0.0.1. Escribe metricas.json y
sale con 1 si algo de eso falla.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import e2e_cdp  # noqa: E402

GUION = "ejemplo_referencias.txt"
ANCHOS = [(400, 900), (1200, 800)]

MEDIR = """(() => {
  const W = window.innerWidth, se = document.scrollingElement;
  const cards = [...document.querySelectorAll('#script-view > .ref-card')]
    .filter(c => c.getBoundingClientRect().width > 0);
  const words = [...document.querySelectorAll('#script-view .w')].map(w => w.getBoundingClientRect());
  const tapadas = [];
  for (const c of cards) {
    const r = c.getBoundingClientRect();
    for (const w of words) {
      if (w.right > r.left + 1 && w.left < r.right - 1 && w.bottom > r.top + 1 && w.top < r.bottom - 1) {
        tapadas.push(c.querySelector('.ref-etiqueta').textContent); break;
      }
    }
  }
  const fuera = [];
  for (const el of document.querySelectorAll('button, input, select, .chip, .barra > *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || el.closest('.hidden')) continue;
    if (r.left < -1 || r.right > W + 1) fuera.push(el.id || el.className || el.tagName);
  }
  return { ancho: W, tarjetas_visibles: cards.length, tapadas, fuera,
           scroll_h: se.scrollWidth > se.clientWidth, refs: TPDebug.refs(),
           vista: document.body.dataset.vista };
})()"""


async def key(cdp, k: str) -> None:
    await cdp.ev(f"document.dispatchEvent(new KeyboardEvent('keydown', {{key: {json.dumps(k)}, bubbles: true}})); true")


async def wait(cdp, expr: str, timeout: float = 15.0) -> None:
    for _ in range(int(timeout * 10)):
        if await cdp.ev(expr):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(expr)


def componer(alfa: Path, fondo: str, out: Path) -> None:
    from PIL import Image, ImageDraw

    top = Image.open(alfa).convert("RGBA")
    base = Image.new("RGBA", top.size, fondo)
    d = ImageDraw.Draw(base)
    # Una «diapositiva»: un título y unas barras, para ver el texto encima.
    tinta = "#FFFFFF" if fondo == "#000000" else "#111111"
    d.rectangle((60, 60, top.width - 60, 120), fill=tinta)
    for i in range(3):
        d.rectangle((120 + i * 260, top.height - 120 - i * 90, 300 + i * 260, top.height - 60), fill="#C0392B")
    Image.alpha_composite(base, top).convert("RGB").save(out, format="PNG", optimize=True)


async def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    port = e2e_cdp.free_port()
    met: dict = {"capturas": []}
    malo: list[str] = []
    with e2e_cdp.server(port):
        async with e2e_cdp.chrome(port) as cdp:
            await e2e_cdp.open_app(cdp, port, 1200, 800)
            await cdp.call("Page.reload")
            await wait(cdp, "!!window.TPDebug")
            # El guion se abre por /control (open_script), como desde el Stream Deck.
            async with websockets.connect(f"ws://127.0.0.1:{port}/control") as ctl:
                await wait(cdp, "document.getElementById('chip-conn').textContent === 'Servidor'")
                await ctl.send(json.dumps({"v": 1, "cmd": "open_script", "value": GUION}))
                await wait(cdp, "TPDebug.state().scriptName === 'ejemplo_referencias.txt'")
                # Al empezar «Resultados»: la diapositiva que viene ya se anuncia.
                await ctl.send(json.dumps({"v": 1, "cmd": "next_section"}))
                await wait(cdp, "TPDebug.state().currentIdx > 0")
                await asyncio.sleep(0.4)
                for w, h in ANCHOS:
                    await cdp.size(w, h)
                    for vista in ("texto", "referencias"):
                        await ctl.send(json.dumps({"v": 1, "cmd": "view", "value": vista}))
                        await wait(cdp, f"document.body.dataset.vista === '{vista}'")
                        await key(cdp, "Escape")
                        await asyncio.sleep(0.3)
                        await cdp.shot(out / f"h85_config_{vista}_{w}.png")
                        await cdp.ev("document.getElementById('btn-preview').click(); true")
                        if vista == "referencias":
                            await wait(cdp, "TPDebug.refs().every(r => r.img || r.codigo || r.kind === 'nota' || r.error)", 60)
                        await cdp.ev("document.getElementById('preview-banner').classList.add('hidden');"
                                     "document.getElementById('notices').replaceChildren(); true")
                        await asyncio.sleep(0.6)
                        m = await cdp.ev(MEDIR)
                        nombre = f"h85_{vista}_{w}.png"
                        await cdp.shot(out / nombre)
                        met["capturas"].append({"archivo": nombre, **m})
                        print(f"{nombre}: tarjetas={m['tarjetas_visibles']} tapadas={m['tapadas']} "
                              f"fuera={m['fuera']} scroll_h={m['scroll_h']}")
                        if m["tapadas"] or m["fuera"] or m["scroll_h"]:
                            malo.append(nombre)
                        if vista == "texto" and m["tarjetas_visibles"]:
                            malo.append(f"{nombre}: tarjetas visibles en Solo texto")
                        if vista == "referencias":
                            errs = [r for r in m["refs"] if r["error"]]
                            if errs:
                                malo.append(f"{nombre}: referencias con error {errs}")
                # Ampliar (Z) a 1200 px.
                await cdp.size(1200, 800)
                await key(cdp, "Escape")
                await cdp.ev("document.getElementById('btn-preview').click(); true")
                await asyncio.sleep(0.4)
                await cdp.ev("document.getElementById('preview-banner').classList.add('hidden'); true")
                await key(cdp, "z")
                await wait(cdp, "!document.getElementById('ref-zoom').classList.contains('hidden')"
                                " && !!document.querySelector('#ref-zoom img')")
                await cdp.shot(out / "h85_ampliar_1200.png")
                await key(cdp, "z")
                # Sobre presentación: transparente y compuesta sobre negra y blanca.
                await key(cdp, "Escape")
                await cdp.ev("document.getElementById('btn-preajuste').click(); true")
                for w, h in ANCHOS:
                    await cdp.size(w, h)
                    await key(cdp, "Escape")
                    await cdp.ev("document.getElementById('btn-preview').click(); true")
                    await asyncio.sleep(0.6)
                    await cdp.ev("document.getElementById('preview-banner').classList.add('hidden'); true")
                    alfa = out / f"h85_sobre_{w}_alfa.png"
                    await cdp.shot(alfa, transparent=True)
                    componer(alfa, "#000000", out / f"h85_sobre_negra_{w}.png")
                    componer(alfa, "#FFFFFF", out / f"h85_sobre_blanca_{w}.png")
                met["prefs_sobre"] = json.loads(await cdp.ev("JSON.stringify(TPDebug.prefs())"))
            met["errores_js"] = cdp.errors
            met["peticiones_fuera"] = cdp.external()
    if met["prefs_sobre"].get("ref_opacidad") != 85:
        malo.append("el preajuste no dejó las referencias al 85 %")
    if met["errores_js"] or met["peticiones_fuera"]:
        malo.append("errores JS o peticiones fuera de 127.0.0.1")
    met["fallos"] = malo
    (out / "metricas.json").write_text(json.dumps(met, ensure_ascii=False, indent=2), encoding="utf-8")
    print("fallos:", malo or "ninguno", "| errores JS:", met["errores_js"], "| fuera:", met["peticiones_fuera"])
    return 1 if malo else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1]))))
