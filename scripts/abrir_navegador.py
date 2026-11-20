"""Modo navegador: arranca el backend en 127.0.0.1:8765 y abre la página cuando el puerto responde.

Lo lanza ABRIR_EN_NAVEGADOR.bat. Cerrar la consola apaga el servidor.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from backend.config import HOST, load_settings  # noqa: E402


def _abrir_cuando_responda(port: int) -> None:
    for _ in range(240):
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                webbrowser.open(f"http://{HOST}:{port}")
                return
        except OSError:
            time.sleep(0.25)


def main() -> None:
    port = load_settings().port
    threading.Thread(target=_abrir_cuando_responda, args=(port,), daemon=True).start()
    uvicorn.run("backend.server:app", host=HOST, port=port)


if __name__ == "__main__":
    main()
