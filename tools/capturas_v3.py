"""Hito 84 (T2 y T3): capturas del frontend nuevo en Chrome headless, servidor real con STT falso.

    .venv312\\Scripts\\python.exe tools\\capturas_v3.py docs\\hito84\\capturas

T2 · ventana adaptable: configuración y lectura a 400/800/1200/1600 x 900 y a 400/800/1600 x 300.
     Mide en la página: controles que se salen, scroll horizontal y palabras por línea.
T3 · sobre presentación: la lectura con el preajuste «Sobre presentación» (fondo 0 %, banda,
     halo) y cada color de texto, capturada con fondo TRANSPARENTE (t3_*_alfa.png). La
     composición sobre diapositiva negra, blanca y una foto la hace tools/componer_t3.py
     (Pillow, Python del sistema). Escribe metricas.json.

Sin red externa: la página solo habla con 127.0.0.1 y la prueba lo comprueba.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import e2e_cdp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GUION = (ROOT / "guiones" / "ejemplo.txt").read_text(encoding="utf-8")
T2 = [(400, 900), (800, 900), (1200, 900), (1600, 900), (400, 300), (800, 300), (1600, 300)]

MEDIR = """(() => {
  const W = window.innerWidth, H = window.innerHeight, fuera = [];
  for (const el of document.querySelectorAll('button, input, select, textarea, .chip, .barra > *')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.closest('.hidden') || getComputedStyle(el).visibility === 'hidden') continue;
    if (r.left < -1 || r.right > W + 1) fuera.push(el.id || el.className || el.tagName);
  }
  const se = document.scrollingElement;
  const lectura = document.body.classList.contains('mode-reader');
  let ppl = null;
  if (lectura) {
    const starts = TPDebug.lineStarts();
    const tot = TPDebug.state().script.total;
    const largos = starts.map((s, i) => (starts[i + 1] ?? tot) - s).filter(n => n > 0);
    largos.sort((a, b) => a - b);
    ppl = largos.length ? { mediana: largos[Math.floor(largos.length / 2)], min: largos[0], max: largos[largos.length - 1] } : null;
  }
  const barra = document.getElementById('barra').getBoundingClientRect();
  return { ancho: W, alto: H, fuera, scroll_h: se.scrollWidth > se.clientWidth,
           palabras_por_linea: ppl, barra_alto: lectura ? Math.round(barra.height) : null,
           compacto: matchMedia('(max-width: 599px)').matches };
})()"""


async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    port = e2e_cdp.free_port()
    metricas: dict = {"t2": [], "t3": []}
    with e2e_cdp.server(port):
        async with e2e_cdp.chrome(port) as cdp:
            await e2e_cdp.open_app(cdp, port)
            await cdp.call("Page.reload")
            await asyncio.sleep(1.0)
            await e2e_cdp.load_script(cdp, GUION)
            for w, h in T2:
                await cdp.size(w, h)
                await cdp.ev("document.body.classList.contains('mode-reader') && document.dispatchEvent("
                             "new KeyboardEvent('keydown', {key: 'Escape'})); true")
                await asyncio.sleep(0.4)
                m_setup = await cdp.ev(MEDIR)
                await cdp.shot(out / f"t2_{w}x{h}_configuracion.png")
                await cdp.ev("document.getElementById('btn-preview').click(); true")
                await asyncio.sleep(0.8)
                m_lect = await cdp.ev(MEDIR)
                await cdp.shot(out / f"t2_{w}x{h}_lectura.png")
                metricas["t2"].append({"tam": f"{w}x{h}", "configuracion": m_setup, "lectura": m_lect})
                print(f"T2 {w}x{h}: config fuera={m_setup['fuera']} scroll_h={m_setup['scroll_h']} | "
                      f"lectura fuera={m_lect['fuera']} scroll_h={m_lect['scroll_h']} ppl={m_lect['palabras_por_linea']}")

            # T3: preajuste y cada color, fondo transparente.
            await cdp.size(1600, 900)
            await cdp.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); true")
            await asyncio.sleep(0.3)
            await cdp.ev("document.getElementById('btn-preajuste').click(); true")
            for color in ("blanco", "marfil", "oro"):
                await cdp.ev(f"document.querySelector('.muestra-color[data-color=\"{color}\"]').click(); true")
                await cdp.ev("document.getElementById('btn-preview').click(); true")
                await asyncio.sleep(0.8)
                # Sin el aviso de vista previa: es lo que se ve leyendo.
                await cdp.ev("document.getElementById('preview-banner').classList.add('hidden'); true")
                await cdp.shot(out / f"t3_{color}_alfa.png", transparent=True)
                p = await cdp.ev("JSON.stringify(TPDebug.prefs())")
                metricas["t3"].append({"color": color, "prefs": json.loads(p)})
                await cdp.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); true")
                await asyncio.sleep(0.3)
            metricas["errores_js"] = cdp.errors
            metricas["peticiones_fuera"] = cdp.external()
    (out / "metricas.json").write_text(json.dumps(metricas, ensure_ascii=False, indent=2), encoding="utf-8")
    print("errores JS:", metricas["errores_js"], "| fuera de 127.0.0.1:", metricas["peticiones_fuera"])
    malo = metricas["errores_js"] or metricas["peticiones_fuera"] or any(
        e[k]["fuera"] or e[k]["scroll_h"] for e in metricas["t2"] for k in ("configuracion", "lectura"))
    sys.exit(1 if malo else 0)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
