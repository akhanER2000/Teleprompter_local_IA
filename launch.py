"""Launcher con ventana nativa "stealth":
- Excluida de captura de pantalla (OBS, Google Meet, Zoom, Win+G, etc.).
- Sin consola visible (lanzar con pythonw.exe o con scripts\\start_stealth.vbs).
- Mover / redimensionar fluido: la display-affinity se aplica sin forzar topmost
  ni reentradas en el message-loop de Edge WebView2.
"""
from __future__ import annotations

# ----------------------------------------------------------------------
# IMPORTANTE: cuando este script se lanza con pythonw.exe, sys.stdout/err
# son None, y cualquier libreria que intente escribir (uvicorn, asyncio,
# logging fallback) lanza una excepcion que puede colgar el hilo del GUI.
# Hay que redirigir ANTES de cualquier otro import.
# ----------------------------------------------------------------------
import os
import sys

if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _devnull
    sys.stderr = _devnull

import asyncio
import ctypes
import logging
import socket
import threading
import time
from pathlib import Path

import webview
import uvicorn

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Silenciar todo el logging para no escribir a stdout (que no existe).
logging.basicConfig(level=logging.CRITICAL, handlers=[logging.NullHandler()])
for name in ("uvicorn", "uvicorn.error", "uvicorn.access",
             "teleprompter", "asyncio", "websockets"):
    log = logging.getLogger(name)
    log.handlers = [logging.NullHandler()]
    log.setLevel(logging.CRITICAL)
    log.propagate = False

from backend.server import app  # noqa: E402


WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDE_FROM_CAPTURE = 0x00000011

WINDOW_TITLE = "Teleprompter Local IA"
HOST = "127.0.0.1"
PORT = int(os.environ.get("TP_PORT", "8765"))

# Transparencia de la ventana (WS_EX_LAYERED + alfa). Verificado con WebView2:
# sigue renderizando en vivo y la exclusión de captura se mantiene.
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x2
MIN_OPACITY = 0.30
# Hito 84 (T3): WebView2 dentro de pywebview 6.2.1 no deja ver lo que hay detrás
# por píxel (medido: transparent=True, TransparencyKey, LWA_COLORKEY, DWM y
# blur-behind dejan el fondo opaco). El único medio que funciona es el alfa de
# TODA la ventana, que también aclara el texto. Por eso el "Fondo" en la app
# nativa baja el alfa de la ventana como mucho hasta este piso: el texto nunca
# queda por debajo del 50 %. En el navegador el fondo CSS sí es independiente.
BACKDROP_FLOOR = 0.50
WINDOW_PREFS = ROOT / "data" / "ventana.json"


def _load_window_prefs() -> dict:
    try:
        import json
        return json.loads(WINDOW_PREFS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_window_prefs(prefs: dict) -> None:
    try:
        import json
        WINDOW_PREFS.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_PREFS.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        pass


def _clamp_opacity(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_OPACITY, min(1.0, v))


def _set_opacity(hwnd, opacity: float) -> bool:
    """Aplica la opacidad a la ventana. 1.0 quita WS_EX_LAYERED por completo."""
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                  ctypes.c_ubyte, ctypes.c_uint]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_int
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if opacity >= 1.0:
        if ex & WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
        return True
    if not ex & WS_EX_LAYERED:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    alpha = int(round(opacity * 255))
    return user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA) != 0


# ----------------------------------------------------------------------
def run_server():
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="critical",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # imprescindible en thread

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    except Exception:
        pass
    finally:
        try:
            loop.close()
        except Exception:
            pass


def wait_for_backend(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def hide_console():
    """Oculta la consola si por alguna razon se lanzo con python.exe."""
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
            try:
                kernel32.FreeConsole()
            except Exception:
                pass
    except Exception:
        pass


# ----------------------------------------------------------------------
# Win32 helpers
# ----------------------------------------------------------------------
def _find_hwnd(title: str):
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    return user32.FindWindowW(None, title)


def _set_affinity(hwnd, affinity: int) -> bool:
    """Aplica WDA_*. Solo display-affinity, jamas topmost: SetWindowPos
    con HWND_TOPMOST combinado con la affinity provoca bloqueos del
    message-loop de Edge WebView2 durante el bucle modal de resize/move,
    lo que hace que Windows marque la ventana como "no responde"."""
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.SetWindowDisplayAffinity.restype = ctypes.c_int
    ok = user32.SetWindowDisplayAffinity(hwnd, affinity)
    if ok == 0 and affinity == WDA_EXCLUDE_FROM_CAPTURE:
        # Fallback en Windows previo a 20H1: WDA_MONITOR oculta el contenido
        # en capturas sin el coste de render del modo EXCLUDE.
        return user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR) != 0
    return ok != 0


HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOSENDCHANGING = 0x0400


def _set_topmost(hwnd, topmost: bool):
    """Toggle topmost OPCIONAL (solo si el usuario lo pide)."""
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos(
        hwnd,
        ctypes.c_void_p(HWND_TOPMOST if topmost else HWND_NOTOPMOST),
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOSENDCHANGING,
    )


# ----------------------------------------------------------------------
class StealthAPI:
    """Puente JS <-> Python disponible como window.pywebview.api.*

    Estado:
      - locked:  True => WDA_EXCLUDE_FROM_CAPTURE activo (anti-captura).
      - topmost: False por defecto. La ventana NO es topmost, lo que
        permite mover/redimensionar de forma fluida.
    """

    def __init__(self):
        self._window: "webview.Window | None" = None  # privado: pywebview no lo expone al puente JS
        self.locked: bool = True
        self.topmost: bool = False
        # Hito 84: el fondo lo decide la página (lo guarda en su localStorage) y
        # se aplica en `pywebviewready`; data/ventana.json ya no manda.
        self.opacity: float = 1.0
        self._hwnd_cache: "int | None" = None
        self._applied_once: bool = False

    def _hwnd(self):
        if self._hwnd_cache:
            return self._hwnd_cache
        h = None
        # El handle nativo identifica NUESTRA ventana; buscar por título
        # podría devolver la de otra instancia con el mismo nombre.
        try:
            h = int(self._window.native.Handle.ToInt32())
        except Exception:
            h = None
        if not h:
            h = _find_hwnd(WINDOW_TITLE)
        if h:
            self._hwnd_cache = h
        return h

    def apply_opacity(self, value) -> float:
        value = _clamp_opacity(value)
        if _set_opacity(self._hwnd(), value):
            self.opacity = value
        return self.opacity

    def apply_lock(self, locked: bool) -> bool:
        hwnd = self._hwnd()
        if not hwnd:
            return False
        affinity = WDA_EXCLUDE_FROM_CAPTURE if locked else WDA_NONE
        applied = _set_affinity(hwnd, affinity)
        self.locked = locked
        if applied:
            self._applied_once = True
        return applied

    def apply_topmost(self, topmost: bool):
        hwnd = self._hwnd()
        if not hwnd:
            return
        _set_topmost(hwnd, topmost)
        self.topmost = topmost

    # Metodos JS ---------------------------------------------------------
    def toggle_lock(self) -> bool:
        self.apply_lock(not self.locked)
        return self.locked

    def toggle_topmost(self) -> bool:
        self.apply_topmost(not self.topmost)
        return self.topmost

    def is_locked(self) -> bool:
        return self.locked

    def is_topmost(self) -> bool:
        return self.topmost

    # Hito 84 ----------------------------------------------------------
    def set_lock(self, locked) -> bool:
        """Invisible en capturas sí/no. Devuelve el estado que quedó."""
        self.apply_lock(bool(locked))
        return self.locked

    def set_topmost(self, topmost) -> bool:
        self.apply_topmost(bool(topmost))
        return self.topmost

    def backdrop_floor(self) -> float:
        return BACKDROP_FLOOR

    def set_backdrop(self, pct) -> float:
        """Fondo 0..100 % -> alfa de la ventana entre BACKDROP_FLOOR y 1,0."""
        try:
            pct = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            pct = 100.0
        return self.apply_opacity(BACKDROP_FLOOR + (1.0 - BACKDROP_FLOOR) * pct / 100.0)

    def set_opacity(self, value, persist=False) -> float:
        """Opacidad de 0.3 a 1.0. Con persist=True se recuerda al reabrir."""
        opacity = self.apply_opacity(value)
        if persist:
            prefs = _load_window_prefs()
            prefs["opacity"] = opacity
            _save_window_prefs(prefs)
        return opacity

    def get_opacity(self) -> float:
        return self.opacity

    def quit_app(self):
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass


# ----------------------------------------------------------------------
def main():
    hide_console()

    # 1) Backend en background
    server_thread = threading.Thread(target=run_server, daemon=True, name="uvicorn")
    server_thread.start()

    if not wait_for_backend():
        webview.create_window(
            WINDOW_TITLE,
            html=("<h1 style='font-family:sans-serif;color:#f33;text-align:center;"
                  "margin-top:40vh'>No se pudo iniciar el backend.</h1>"),
        )
        webview.start()
        return

    # 2) Ventana nativa.
    #    on_top=False y topmost desactivado por defecto: la ventana se puede
    #    mover y redimensionar fluidamente. La proteccion anti-captura se
    #    aplica sin afectar al message-loop.
    api = StealthAPI()
    window = webview.create_window(
        WINDOW_TITLE,
        f"http://{HOST}:{PORT}",
        width=1280,
        height=800,
        min_size=(360, 260),   # T2: se prueba hasta 400 x 300
        resizable=True,
        on_top=False,
        js_api=api,
        background_color="#050505",
        confirm_close=False,
        text_select=True,
    )
    api._window = window

    # 3) Aplicar stealth UNA sola vez tras `shown`.
    fired = threading.Event()

    def _apply_initial_stealth():
        # Espera muy breve para que Windows asigne el HWND final.
        for delay in (0.05, 0.1, 0.2, 0.3):
            if api.apply_lock(True):
                if api.opacity < 1.0:
                    api.apply_opacity(api.opacity)  # transparencia guardada
                return
            time.sleep(delay)
        # Si tras 0.65s no se pudo aplicar, no insistimos: evitamos consumir
        # CPU en un loop y dejamos la ventana responsiva.

    def on_shown():
        if fired.is_set():
            return
        fired.set()
        threading.Thread(
            target=_apply_initial_stealth,
            daemon=True,
            name="stealth-init",
        ).start()

    window.events.shown += on_shown
    window.events.loaded += on_shown

    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
