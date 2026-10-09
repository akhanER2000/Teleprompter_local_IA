"""Protocolo WebSocket v2: valida los mensajes del cliente y construye los del servidor.

Contrato en docs/diseno_v2.md, sección 6. Un mensaje inválido lanza
`ProtocolError` (código `BAD_REQUEST`); el servidor lo responde con un
`status` de error y la conexión sigue abierta.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Union

from tools.script_parser import MAX_SCRIPT_CHARS

# Tras `json.loads`, un code point sustituto en un str siempre está suelto
# (JSON.stringify escribe así un emoji partido): no se puede codificar en UTF-8.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")

PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_SEQ = 2**53 - 1          # mayor entero exacto en JavaScript
LEVEL_FLOOR_DB = -120.0      # piso del medidor (JSON no admite -Infinity)

MODEL_STATES = frozenset({"loading", "ready", "error"})
STATUS_STATES = frozenset({"loading", "ready", "listening", "paused", "desync", "error", "warning"})
CODES = frozenset({"BAD_REQUEST", "NO_SCRIPT", "MODEL_NOT_READY", "MODEL_ERROR",
                   "MODEL_MISSING", "MIC_ERROR", "MIC_LOST", "CUDA_FALLBACK", "INTERNAL"})


class ProtocolError(ValueError):
    """Mensaje del cliente inválido; `message` va en español para mostrarlo."""

    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- cliente

@dataclass(frozen=True)
class Hello:
    action: ClassVar[str] = "hello"
    client: str | None = None


@dataclass(frozen=True)
class LoadScript:
    action: ClassVar[str] = "load_script"
    text: str
    seq: int | None = None


@dataclass(frozen=True)
class Start:
    action: ClassVar[str] = "start"
    idx: int | None = None     # None: conserva la posición actual
    seq: int | None = None


@dataclass(frozen=True)
class Pause:
    action: ClassVar[str] = "pause"


@dataclass(frozen=True)
class SetPosition:
    action: ClassVar[str] = "set_position"
    idx: int
    seq: int | None = None


@dataclass(frozen=True)
class SetInputDevice:  # Fase 6
    action: ClassVar[str] = "set_input_device"
    device: int | None = None  # None: dispositivo predeterminado


@dataclass(frozen=True)
class MicTest:  # Fase 6
    action: ClassVar[str] = "mic_test"
    on: bool = False


ClientMessage = Union[Hello, LoadScript, Start, Pause, SetPosition, SetInputDevice, MicTest]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _seq(data: dict) -> int | None:
    if "seq" not in data or data["seq"] is None:
        return None
    seq = data["seq"]
    if not _is_int(seq) or not 0 <= seq <= MAX_SEQ:
        raise ProtocolError("'seq' debe ser un entero mayor o igual que 0.")
    return seq


def _idx(data: dict, total_tokens: int | None, *, required: bool) -> int | None:
    if data.get("idx") is None:
        if required:
            raise ProtocolError("Falta 'idx' (entero).")
        return None
    idx = data["idx"]
    if not _is_int(idx):
        raise ProtocolError("'idx' debe ser un entero.")
    if total_tokens is None:
        # Sin guion solo se exige idx >= 0: el servidor responde NO_SCRIPT.
        if not 0 <= idx <= MAX_SEQ:
            raise ProtocolError("'idx' debe ser un entero mayor o igual que 0.")
    elif not 0 <= idx <= total_tokens:
        raise ProtocolError(f"'idx' fuera de rango: {idx} (válido: 0 a {total_tokens}).")
    return idx


def _parse_hello(data: dict, total_tokens: int | None) -> Hello:
    client = data.get("client")
    if not isinstance(client, str):
        return Hello(client=None)
    return Hello(client=_SURROGATE_RE.sub("�", client[:100]))  # solo va al log


def _parse_load_script(data: dict, total_tokens: int | None) -> LoadScript:
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("'text' debe ser un texto no vacío.")
    if len(text) > MAX_SCRIPT_CHARS:
        raise ProtocolError(f"El guion supera el máximo de {MAX_SCRIPT_CHARS} caracteres.")
    return LoadScript(text=text, seq=_seq(data))


def _parse_start(data: dict, total_tokens: int | None) -> Start:
    return Start(idx=_idx(data, total_tokens, required=False), seq=_seq(data))


def _parse_pause(data: dict, total_tokens: int | None) -> Pause:
    return Pause()


def _parse_set_position(data: dict, total_tokens: int | None) -> SetPosition:
    return SetPosition(idx=_idx(data, total_tokens, required=True), seq=_seq(data))


def _parse_set_input_device(data: dict, total_tokens: int | None) -> SetInputDevice:
    device = data.get("device")
    if device is not None and (not _is_int(device) or device < 0):
        raise ProtocolError("'device' debe ser un entero mayor o igual que 0, o null.")
    return SetInputDevice(device=device)


def _parse_mic_test(data: dict, total_tokens: int | None) -> MicTest:
    on = data.get("on")
    if not isinstance(on, bool):
        raise ProtocolError("'on' debe ser true o false.")
    return MicTest(on=on)


_PARSERS = {
    "hello": _parse_hello,
    "load_script": _parse_load_script,
    "start": _parse_start,
    "pause": _parse_pause,
    "set_position": _parse_set_position,
    "set_input_device": _parse_set_input_device,
    "mic_test": _parse_mic_test,
}
ACTIONS = frozenset(_PARSERS)


def _too_big(raw: str) -> bool:
    if len(raw) > MAX_MESSAGE_BYTES:
        return True
    # Hasta 4 bytes por carácter en UTF-8: solo se mide si podría pasarse.
    return len(raw) * 4 > MAX_MESSAGE_BYTES and len(raw.encode("utf-8", "surrogatepass")) > MAX_MESSAGE_BYTES


def parse_client_message(raw: str | bytes, total_tokens: int | None) -> ClientMessage:
    """Valida un mensaje del cliente y devuelve su objeto tipado.

    `total_tokens` es el total del guion cargado (rango de `idx`), o None si
    no hay guion. Los campos desconocidos se ignoran. Lanza `ProtocolError`.
    """
    if not isinstance(raw, str):
        raise ProtocolError("Solo se aceptan mensajes de texto con JSON.")
    if _too_big(raw):
        raise ProtocolError("El mensaje supera el máximo de 4 MB.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProtocolError("El mensaje no es JSON válido.") from None
    if not isinstance(data, dict):
        raise ProtocolError("El mensaje debe ser un objeto JSON.")
    action = data.get("action")
    if not isinstance(action, str):
        raise ProtocolError("Falta 'action' (texto).")
    parser = _PARSERS.get(action)
    if parser is None:
        raise ProtocolError(f"Acción desconocida: {action[:40]!r}.")
    return parser(data, total_tokens)


# ---------------------------------------------------------------- servidor

def _finite(value: float, default: float) -> float:
    value = float(value)
    return value if math.isfinite(value) else default


def state_msg(*, model: str, model_name: str, device: str, listening: bool,
              script_id: str | None, total_tokens: int, current_idx: int,
              epoch: int, debug: bool) -> dict:
    if model not in MODEL_STATES:
        raise ValueError(f"estado de modelo desconocido: {model!r}")
    return {"type": "state", "protocol": PROTOCOL_VERSION, "model": model,
            "model_name": model_name, "device": device, "listening": bool(listening),
            "script_id": script_id, "total_tokens": int(total_tokens),
            "current_idx": int(current_idx), "epoch": int(epoch), "debug": bool(debug)}


def script_loaded_msg(*, script_id: str, total_tokens: int, epoch: int,
                      text: str, segments: list[dict]) -> dict:
    return {"type": "script_loaded", "script_id": script_id, "total_tokens": int(total_tokens),
            "epoch": int(epoch), "text": text, "segments": segments}


def scroll_msg(*, current_idx: int, epoch: int, confidence: float = 1.0,
               matched_word: str | None = None, stt_ms: int = 0, lag_ms: int = 0,
               resync: bool = False) -> dict:
    confidence = min(1.0, max(0.0, _finite(confidence, 0.0)))
    return {"type": "scroll", "current_idx": int(current_idx), "epoch": int(epoch),
            "confidence": round(confidence, 3), "matched_word": matched_word,
            "stt_ms": max(0, int(stt_ms)), "lag_ms": max(0, int(lag_ms)), "resync": bool(resync)}


def echo_scroll_msg(current_idx: int, epoch: int) -> dict:
    """Eco de un reposicionamiento (set_position o start)."""
    return scroll_msg(current_idx=current_idx, epoch=epoch)


def status_msg(state: str, message: str, code: str | None = None) -> dict:
    if state not in STATUS_STATES:
        raise ValueError(f"estado desconocido: {state!r}")
    if code is not None and code not in CODES:
        raise ValueError(f"código desconocido: {code!r}")
    return {"type": "status", "state": state, "code": code, "message": message}


def level_msg(rms_db: float) -> dict:
    return {"type": "level", "rms_db": round(max(LEVEL_FLOOR_DB, _finite(rms_db, LEVEL_FLOOR_DB)), 1)}


def transcript_msg(text: str, is_final: bool = False) -> dict:
    return {"type": "transcript", "text": text, "is_final": bool(is_final)}


def encode(msg: dict) -> str:
    """JSON compacto que siempre se puede enviar como UTF-8.

    Rechaza NaN e infinitos (JSON.parse no los acepta). Si algún texto trae un
    sustituto suelto (no codificable en UTF-8, y el transporte fallaría al
    enviarlo), se serializa con escapes `\\uXXXX`: JSON.parse en el navegador
    restaura la misma cadena.
    """
    text = json.dumps(msg, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if not text.isascii():
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            text = json.dumps(msg, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return text
