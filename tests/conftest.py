"""Configuración común de pytest.

Las pruebas corren sin GPU, sin micrófono y sin red: se fuerza el STT y el
audio falsos antes de importar cualquier módulo del backend.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TP_FAKE_STT", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
