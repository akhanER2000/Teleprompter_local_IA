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
PORT = 8765


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
        self.window: "webview.Window | None" = None
        self.locked: bool = True
        self.topmost: bool = False
        self._hwnd_cache: "int | None" = None
        self._applied_once: bool = False

    def _hwnd(self):
        if self._hwnd_cache:
            return self._hwnd_cache
        h = _find_hwnd(WINDOW_TITLE)
        if h:
            self._hwnd_cache = h
        return h

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

    def quit_app(self):
        if self.window is not None:
            try:
                self.window.destroy()
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
        min_size=(640, 400),
        resizable=True,
        on_top=False,
        js_api=api,
        background_color="#050505",
        confirm_close=False,
        text_select=True,
    )
    api.window = window

    # 3) Aplicar stealth UNA sola vez tras `shown`.
    fired = threading.Event()

    def _apply_initial_stealth():
        # Espera muy breve para que Windows asigne el HWND final.
        for delay in (0.05, 0.1, 0.2, 0.3):
            if api.apply_lock(True):
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
