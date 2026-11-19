"""Valida frontend/tema.css en modo navegador (Chrome headless por CDP, sin red externa).

    set TP_FAKE_STT=1 & set TP_PORT=8779 & .venv312\\Scripts\\python.exe -m uvicorn backend.server:app --port 8779
    .venv312\\Scripts\\python.exe -I docs\\diseno_v3\\validar_tema.py 8779 docs\\diseno_v3\\capturas

Para cada tamaño (1600x900 y 400x700) captura la configuración y la lectura dos veces: sin el tema (si index.html
ya lo enlaza, se desactiva esa hoja) y con él.
Si `index.html` aún no enlaza `/static/tema.css`, lo inyecta por CDP y lo dice. Si la lógica aún no escribe los
atributos `data-*` del contrato (CLASES.md), los pone con los valores por defecto del diseño y lo dice.

Falla (exit 1) si: tema.css no responde 200, la hoja no carga o no tiene reglas, hay errores de consola o
excepciones, o la página pide algo fuera de 127.0.0.1 que no sea Google Fonts (que V0 ya marcó y se bloquea).
"""
import asyncio, base64, json, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
import websockets

PORT, OUT = int(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DBG = 9400 + PORT % 100
GUION = Path(__file__).resolve().parents[2] / "guiones" / "ejemplo.txt"
TAMANOS = [(1600, 900), (400, 700)]

INYECTAR = """new Promise(res => {
  let l = document.querySelector('link[href*="tema.css"]');
  if (l) { res({inyectado: false, reglas: l.sheet ? l.sheet.cssRules.length : -1}); return; }
  l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/static/tema.css';
  l.onload = () => res({inyectado: true, reglas: l.sheet.cssRules.length});
  l.onerror = () => res({inyectado: true, reglas: -1, error: true});
  document.head.appendChild(l);
})"""

ATRIBUTOS = """(() => {
  const b = document.body, puestos = [];
  const def = {modo: 'manual', marcha: 'corriendo', banda: 'on', halo: 'off', texto: 'marfil'};
  for (const [k, v] of Object.entries(def)) if (!b.dataset[k]) { b.dataset[k] = v; puestos.push(k); }
  return puestos;
})()"""

MEDIR = """(() => {
  const sv = document.getElementById('script-view'), cs = sv ? getComputedStyle(sv) : null;
  const cur = document.querySelector('.script-view .w.current');
  return {fuente: cs && cs.fontFamily, peso: cs && cs.fontWeight, color: cs && cs.color,
          fondo_lectura: getComputedStyle(document.querySelector('.reader')).backgroundColor,
          subrayado: cur ? getComputedStyle(cur).textDecorationColor : null,
          fuentes_externas: [...document.querySelectorAll('link[href^="http"]')].map(l => l.href.slice(0, 50))};
})()"""


async def main():
    tema = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/static/tema.css")
    estado_tema = tema.status
    prof = tempfile.mkdtemp(prefix="validar_tema_")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={DBG}", f"--user-data-dir={prof}",
                             "--no-first-run", "--hide-scrollbars", "about:blank"])
    fallos, informe = [], {"tema_css_http": estado_tema, "pantallas": []}
    if estado_tema != 200: fallos.append(f"tema.css respondió {estado_tema}")
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DBG}/json")); break
            except Exception:
                time.sleep(0.2)
        url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        async with websockets.connect(url, max_size=None) as ws:
            n, eventos = 0, []
            async def cdp(method, **params):
                nonlocal n; n += 1; mid = n
                await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == mid:
                        if "error" in m: raise RuntimeError(m["error"])
                        return m.get("result", {})
                    if "method" in m: eventos.append(m)
            async def ev(expr):
                r = await cdp("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
                return r.get("result", {}).get("value")
            async def foto(nombre):
                s = await cdp("Page.captureScreenshot", format="png")
                (OUT / nombre).write_bytes(base64.b64decode(s["data"]))
            for dom in ("Network", "Page", "Runtime", "Log"): await cdp(f"{dom}.enable")
            await cdp("Network.setBlockedURLs", urls=["*googleapis.com*", "*gstatic.com*"])
            texto = GUION.read_text(encoding="utf-8")
            for w, h in TAMANOS:
                await cdp("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
                for con_tema in (False, True):
                    eventos.clear()
                    await cdp("Page.navigate", url=f"http://127.0.0.1:{PORT}/")
                    await asyncio.sleep(1.5)
                    etiq = f"{'tema' if con_tema else 'actual'}_{w}x{h}"
                    pant = {"pantalla": etiq}
                    if not con_tema:   # referencia: la misma página sin el tema (si index.html ya lo enlaza)
                        pant["tema_desactivado"] = await ev(
                            "(() => { const l = document.querySelector('link[href*=\"tema.css\"]');"
                            " if (l) l.disabled = true; return !!l; })()")
                    if con_tema:
                        pant["carga"] = await ev(INYECTAR)
                        pant["atributos_puestos_por_la_prueba"] = await ev(ATRIBUTOS)
                        if pant["carga"].get("reglas", -1) <= 0: fallos.append(f"{etiq}: tema.css sin reglas")
                    await asyncio.sleep(0.4)
                    await foto(f"{etiq}_configuracion.png")
                    await ev(f"document.getElementById('script').value = {json.dumps(texto)};"
                             "document.getElementById('btn-load').click(); true")
                    await asyncio.sleep(1.5)
                    await ev("document.getElementById('btn-auto').click(); true")
                    await asyncio.sleep(3.0)
                    await ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', code: 'KeyA', bubbles: true})); true")
                    await asyncio.sleep(0.6)
                    pant["medidas"] = await ev(MEDIR)
                    await foto(f"{etiq}_lectura.png")
                    errores = [e["params"]["entry"]["text"] for e in eventos if e["method"] == "Log.entryAdded"
                               and e["params"]["entry"]["level"] == "error"
                               and "googleapis" not in e["params"]["entry"].get("url", "")
                               and "gstatic" not in e["params"]["entry"].get("url", "")]
                    excep = [e["params"]["exceptionDetails"].get("text") for e in eventos
                             if e["method"] == "Runtime.exceptionThrown"]
                    consola = [a["value"] if "value" in a else a.get("description") for e in eventos
                               if e["method"] == "Runtime.consoleAPICalled" and e["params"]["type"] == "error"
                               for a in e["params"]["args"]]
                    ext = sorted({e["params"]["request"]["url"][:60] for e in eventos
                                  if e["method"] == "Network.requestWillBeSent"
                                  and not e["params"]["request"]["url"].startswith(("http://127.0.0.1", "ws://127.0.0.1", "data:"))})
                    pant.update(errores_log=errores, excepciones=excep, consola_error=consola, peticiones_fuera=ext)
                    if con_tema and (errores or excep or consola):
                        fallos.append(f"{etiq}: errores {errores + excep + consola}")
                    if [u for u in ext if "googleapis" not in u and "gstatic" not in u]:
                        fallos.append(f"{etiq}: peticiones fuera de 127.0.0.1 {ext}")
                    informe["pantallas"].append(pant)
                    print(json.dumps(pant, ensure_ascii=False), flush=True)
    finally:
        proc.terminate()
    informe["fallos"] = fallos
    (OUT / "validacion.json").write_text(json.dumps(informe, ensure_ascii=False, indent=1), encoding="utf-8")
    print("FALLOS:" if fallos else "OK: el tema carga sin errores", *fallos, sep="\n")
    sys.exit(1 if fallos else 0)

asyncio.run(main())
