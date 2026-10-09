"""Única fuente de configuración: variables `TP_*`, `config.json` y defectos.

Precedencia: variable de entorno, luego `config.json` en la raíz (opcional,
ignorado por git), luego el valor por defecto. Un valor inválido se registra
como advertencia y se reemplaza por el defecto: la carga nunca lanza
(docs/diseno_v2.md, sección 10).

`config.json` acepta como claves los nombres de campo de `Settings`
(`"model_size": "small"`) o los nombres de variable (`"TP_MODEL_SIZE": "small"`).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("teleprompter.config")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
SCRIPTS_DIR = ROOT / "guiones"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"

HOST = "127.0.0.1"  # nunca se expone fuera de la máquina

DEVICES = ("cuda", "cpu")
# Tipos de cómputo que acepta CTranslate2 4.x.
COMPUTE_TYPES = ("default", "auto", "int8", "int8_float32", "int8_float16",
                 "int8_bfloat16", "int16", "float16", "bfloat16", "float32")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRUE = frozenset({"1", "true", "yes", "on", "si", "sí"})
_FALSE = frozenset({"0", "false", "no", "off"})
_LANG_RE = re.compile(r"[a-z]{2,3}")


@dataclass(frozen=True)
class Settings:
    """Configuración efectiva e inmutable. El host no es configurable."""

    port: int = 8765
    model_size: str = "medium"
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "es"
    input_device: int | None = None
    window_s: float = 3.0
    step_ms: int = 350
    prompt_context: bool = False
    vad_threshold: float = 0.5
    debug: bool = False
    fake_stt: bool = False
    audio_file: str | None = None
    log_level: str = "INFO"

    @property
    def host(self) -> str:
        return HOST


# ---------------------------------------------------------------- parsers
# Cada parser recibe un str (entorno) o un valor JSON y lanza ValueError si
# no es válido.

def _int(lo: int, hi: int) -> Callable[[Any], int]:
    def parse(raw: Any) -> int:
        if isinstance(raw, bool):
            raise ValueError("se esperaba un entero")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and re.fullmatch(r"[+-]?\d+", raw.strip()):
            value = int(raw.strip())
        else:
            raise ValueError("se esperaba un entero")
        if not lo <= value <= hi:
            raise ValueError(f"fuera de rango ({lo} a {hi})")
        return value
    return parse


def _float(lo: float, hi: float, *, open_lo: bool = False) -> Callable[[Any], float]:
    def parse(raw: Any) -> float:
        if isinstance(raw, bool):
            raise ValueError("se esperaba un número")
        if isinstance(raw, (int, float)):
            value = float(raw)
        elif isinstance(raw, str):
            try:  # acepta coma decimal ("2,5")
                value = float(raw.strip().replace(",", "."))
            except ValueError:
                raise ValueError("se esperaba un número") from None
        else:
            raise ValueError("se esperaba un número")
        if not math.isfinite(value):
            raise ValueError("se esperaba un número finito")
        if value > hi or value < lo or (open_lo and value == lo):
            interval = f"({lo} a {hi}]" if open_lo else f"[{lo} a {hi}]"
            raise ValueError(f"fuera de rango {interval}")
        return value
    return parse


def _bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise ValueError("se esperaba 1/0, true/false, yes/no u on/off")


def _choice(options: tuple[str, ...], *, upper: bool = False) -> Callable[[Any], str]:
    def parse(raw: Any) -> str:
        if not isinstance(raw, str):
            raise ValueError("se esperaba texto")
        text = raw.strip().upper() if upper else raw.strip().lower()
        if text not in options:
            raise ValueError("opciones válidas: " + ", ".join(options))
        return text
    return parse


def _text(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("se esperaba texto no vacío")
    return raw.strip()


def _language(raw: Any) -> str:
    text = _text(raw).lower()
    if not _LANG_RE.fullmatch(text):
        raise ValueError("se esperaba un código de idioma de Whisper (por ejemplo 'es')")
    return text


def _input_device(raw: Any) -> int | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None  # dispositivo predeterminado
    return _int(0, 4096)(raw)


def _audio_file(raw: Any) -> str | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    path = Path(_text(raw)).expanduser()
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, ROOT / path]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise ValueError("el archivo no existe")


# campo -> (variable de entorno, parser)
_SPECS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "port": ("TP_PORT", _int(1, 65535)),
    "model_size": ("TP_MODEL_SIZE", _text),
    "device": ("TP_DEVICE", _choice(DEVICES)),
    "compute_type": ("TP_COMPUTE", _choice(COMPUTE_TYPES)),
    "language": ("TP_LANGUAGE", _language),
    "input_device": ("TP_INPUT_DEVICE", _input_device),
    "window_s": ("TP_WINDOW_S", _float(1.0, 30.0)),
    "step_ms": ("TP_STEP_MS", _int(20, 5000)),
    "prompt_context": ("TP_PROMPT_CONTEXT", _bool),
    "vad_threshold": ("TP_VAD_THRESHOLD", _float(0.0, 1.0, open_lo=True)),
    "debug": ("TP_DEBUG", _bool),
    "fake_stt": ("TP_FAKE_STT", _bool),
    "audio_file": ("TP_AUDIO_FILE", _audio_file),
    "log_level": ("TP_LOG_LEVEL", _choice(LOG_LEVELS, upper=True)),
}
_FIELD_BY_VAR = {var: name for name, (var, _parse) in _SPECS.items()}


def _read_config_file(path: Path) -> dict[str, tuple[str, Any]]:
    """Valores de `config.json` por campo, con la clave original. {} si falta o es inválido."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.warning("No se pudo leer %s (%s); se ignora.", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("%s debe contener un objeto JSON; se ignora.", path)
        return {}
    values: dict[str, tuple[str, Any]] = {}
    for key, value in data.items():
        name = key if key in _SPECS else _FIELD_BY_VAR.get(key)
        if name is None:
            log.warning("Clave desconocida en %s: %r; se ignora.", path.name, key)
            continue
        if name in values:
            log.warning("%s define %r dos veces (%r y %r); se usa %r.",
                        path.name, name, values[name][0], key, key)
        values[name] = (key, value)
    return values


def load_settings(env: Mapping[str, str] | None = None,
                  config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Lee la configuración efectiva. Nunca lanza por un valor inválido.

    Una variable de entorno vacía cuenta como no definida.
    """
    env = os.environ if env is None else env
    file_values = _read_config_file(Path(config_path) if config_path else CONFIG_PATH)
    defaults = Settings()
    values: dict[str, Any] = {}
    for name, (var, parse) in _SPECS.items():
        default = getattr(defaults, name)
        raw = env.get(var)
        if raw is not None and raw.strip():
            source = var
        elif name in file_values:
            key, raw = file_values[name]
            source = f"config.json[{key!r}]"
        else:
            values[name] = default
            continue
        try:
            values[name] = parse(raw)
        except (ValueError, TypeError) as exc:
            log.warning("Valor inválido en %s: %r (%s). Se usa el defecto %r.",
                        source, raw, exc, default)
            values[name] = default
    return Settings(**values)
