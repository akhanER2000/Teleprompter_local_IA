"""Corre las pruebas de Node del frontend (tests/frontend/*.test.js) desde pytest.

Si Node no está instalado la prueba se SALTA y lo dice: no da verde sin correr.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_node_frontend() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está instalado: las pruebas de frontend/core.js no se corrieron.")
    files = sorted(str(p) for p in (ROOT / "tests" / "frontend").glob("*.test.js"))
    assert files, "no hay pruebas de frontend"
    proc = subprocess.run([node, "--test", *files], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def test_app_js_sintaxis() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node no está instalado.")
    for name in ("core.js", "app.js"):
        proc = subprocess.run([node, "--check", str(ROOT / "frontend" / name)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
