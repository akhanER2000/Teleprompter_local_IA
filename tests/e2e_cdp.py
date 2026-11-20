"""Ayudas para pruebas de extremo a extremo: servidor real + Chrome headless por CDP.

Sin red externa: el servidor escucha en 127.0.0.1 con STT falso y Chrome bloquea
cualquier URL que no sea 127.0.0.1. Lo usan tests/test_e2e_control.py y
tools/capturas_v3.py.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable)
CHROMES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_chrome() -> str | None:
    for c in CHROMES:
        if Path(c).is_file():
            return c
    return shutil.which("chrome") or shutil.which("msedge")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"el puerto {port} no respondió")


@contextlib.contextmanager
def server(port: int, **env):
    """Backend real (uvicorn) con STT falso salvo que `env` diga otra cosa."""
    e = {**os.environ, "TP_FAKE_STT": "1", "TP_PORT": str(port), "HF_HUB_OFFLINE": "1", **env}
    proc = subprocess.Popen([str(PY), "-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=e)
    try:
        wait_port(port)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


class Cdp:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.n = 0
        self.urls: list[str] = []      # cada petición de red que hizo la página
        self.errors: list[str] = []    # excepciones de JavaScript

    def _event(self, m: dict) -> None:
        method = m.get("method")
        if method == "Network.requestWillBeSent":
            self.urls.append(m["params"]["request"]["url"])
        elif method == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            self.errors.append(str(d.get("exception", {}).get("description") or d.get("text")))

    def external(self) -> list[str]:
        """Peticiones fuera de 127.0.0.1 (regla 1: 100 % local)."""
        # blob:http://127.0.0.1 es memoria de la propia página (miniaturas del hito 85), no red.
        return [u for u in self.urls if not u.startswith(("http://127.0.0.1", "ws://127.0.0.1", "data:", "about:",
                                                           "blob:http://127.0.0.1"))]

    async def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if "method" in m:
                self._event(m)
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    async def ev(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r.get("result", {}).get("value")

    async def size(self, w: int, h: int) -> None:
        await self.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                        deviceScaleFactor=1, mobile=False)

    async def shot(self, path: Path, *, transparent: bool = False) -> None:
        if transparent:
            await self.call("Emulation.setDefaultBackgroundColorOverride",
                            color={"r": 0, "g": 0, "b": 0, "a": 0})
        r = await self.call("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))
        if transparent:
            await self.call("Emulation.setDefaultBackgroundColorOverride")


@contextlib.asynccontextmanager
async def chrome(app_port: int):
    exe = find_chrome()
    if exe is None:
        raise FileNotFoundError("no hay Chrome ni Edge")
    dbg = free_port()
    # Perfil fijo en %TEMP% (se reutiliza; nada se borra).
    prof = str(Path(tempfile.gettempdir()) / "tp_e2e_chrome")
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={dbg}",
                             f"--user-data-dir={prof}", "--no-first-run", "--hide-scrollbars",
                             "--disable-extensions", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json"))
                break
            except Exception:
                await asyncio.sleep(0.1)
        url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        async with websockets.connect(url, max_size=None) as ws:
            cdp = Cdp(ws)
            await cdp.call("Network.enable")
            # 100 % local: se registra cada petición (cdp.external()) y lo externo conocido se bloquea.
            await cdp.call("Network.setBlockedURLs", urls=["*googleapis.com*", "*gstatic.com*", "https://*"])
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            yield cdp
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(10)


async def open_app(cdp: Cdp, port: int, w: int = 1280, h: int = 800) -> None:
    await cdp.size(w, h)
    await cdp.call("Page.navigate", url=f"http://127.0.0.1:{port}/")
    for _ in range(100):
        if await cdp.ev("document.readyState === 'complete' && !!window.TPCore"):
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError(f"la página no cargó core.js; errores: {cdp.errors}")
    # Preferencias limpias en cada prueba.
    await cdp.ev("localStorage.clear(); true")


async def load_script(cdp: Cdp, text: str) -> None:
    await cdp.ev(f"document.getElementById('script').value = {json.dumps(text)};"
                 "document.getElementById('btn-load').click(); true")
    for _ in range(100):
        if await cdp.ev("document.querySelectorAll('#script-view [data-idx]').length > 0"):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("el guion no se dibujó")
