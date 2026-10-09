"""El entorno instalado coincide con las versiones fijadas en requirements.txt."""
from __future__ import annotations

import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _pins(path: Path) -> list[tuple[str, str]]:
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?==([^\s;]+)", line)
        if m:
            pins.append((m.group(1), m.group(3)))
    return pins


@pytest.mark.parametrize("name,expected", _pins(ROOT / "requirements.txt"))
def test_version_fijada_instalada(name: str, expected: str) -> None:
    try:
        installed = version(name)
    except PackageNotFoundError:
        pytest.fail(f"{name} no está instalado (se esperaba {expected})")
    assert installed == expected, f"{name}: instalado {installed}, fijado {expected}"


def test_python_312_o_superior() -> None:
    assert sys.version_info >= (3, 12)


def test_sin_torch() -> None:
    with pytest.raises(PackageNotFoundError):
        version("torch")
