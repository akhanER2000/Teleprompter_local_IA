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


VOICE_MODES = frozenset({"voz", "asistido"})


@dataclass(frozen=True)
class Start:
    action: ClassVar[str] = "start"
    idx: int | None = None     # None: conserva la posición actual
    seq: int | None = None
    mode: str | None = None  # hito 84: "voz" (con frenos), "asistido"; None = v2 sin frenos
    brakes: dict | None = None  # frenos de Voz validados (BrakeParams); None: los de siempre


@dataclass(frozen=True)
class Estado:
    """Hito 84: el visor informa su estado para los controles (/control)."""
    action: ClassVar[str] = "estado"
    fields: dict


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


ClientMessage = Union[Hello, LoadScript, Start, Pause, SetPosition, SetInputDevice, MicTest, Estado]


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


_BRAKE_KEYS = {"confirm_words": int, "max_ahead": int, "max_wps": float, "sentence_guard": bool,
               "confirm_hold_s": float, "agree_slack": int, "anchor_window_s": float}


def _brakes(data: dict) -> dict | None:
    raw = data.get("brakes")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProtocolError("'brakes' debe ser un objeto.")
    from tools.voice_brakes import LIMITS  # sin dependencias pesadas
    out: dict = {}
    for key, kind in _BRAKE_KEYS.items():
        if key not in raw:
            continue
        value = raw[key]
        if kind is bool:
            if not isinstance(value, bool):
                raise ProtocolError(f"'brakes.{key}' debe ser true o false.")
        else:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or (kind is int and value != int(value))):
                raise ProtocolError(f"'brakes.{key}' debe ser un número.")
            lo, hi = LIMITS[key]
            if not lo <= value <= hi:
                raise ProtocolError(f"'brakes.{key}' fuera de rango: {value} (válido: {lo} a {hi}).")
            value = kind(value)
        out[key] = value
    return out


def _parse_start(data: dict, total_tokens: int | None) -> Start:
    mode = data.get("mode")
    if mode is not None and mode not in VOICE_MODES:
        raise ProtocolError("'mode' debe ser \"voz\" o \"asistido\".")
    return Start(idx=_idx(data, total_tokens, required=False), seq=_seq(data), mode=mode,
                 brakes=_brakes(data))


def _parse_estado(data: dict, total_tokens: int | None) -> Estado:
    fields = data.get("estado")
    if not isinstance(fields, dict):
        raise ProtocolError("'estado' debe ser un objeto.")
    return Estado(fields=sanitize_estado(fields))


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
    "estado": _parse_estado,
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
                      text: str, segments: list[dict], name: str | None = None) -> dict:
    return {"type": "script_loaded", "script_id": script_id, "total_tokens": int(total_tokens),
            "epoch": int(epoch), "text": text, "segments": segments, "name": name}


def activity_msg(speaking: bool, silence_ms: int) -> dict:
    """Hito 84 (Asistido): si hay voz, y desde cuándo calla (cota inferior, en ms)."""
    return {"type": "activity", "speaking": bool(speaking), "silence_ms": max(0, int(silence_ms))}


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


# ---------------------------------------------------------------- /control (hito 84, T6)
# Contrato fijo en Profiles_StreamDeck/kit_Galleon100SD/docs/plan84/DECISIONES_84.md (T6).

CONTROL_VERSION = 1
CONTROL_MAX_BYTES = 4096
CONTROL_MODES = ("manual", "asistido", "voz", "siguiente")
# Hito 85 (R5): se suman a T6 sin cambiar lo que existe.
CONTROL_VIEWS = ("texto", "referencias", "siguiente")


@dataclass(frozen=True)
class ControlCmd:
    cmd: str
    value: Any = None


def _ctl_int(value: Any, lo: int, hi: int, name: str, *, nonzero: bool = False) -> int:
    if not _is_int(value) or not lo <= value <= hi or (nonzero and value == 0):
        extra = " y distinto de 0" if nonzero else ""
        raise ProtocolError(f"'value' de '{name}' debe ser un entero de {lo} a {hi}{extra}.")
    return value


def _ctl_none(cmd: str, value: Any) -> None:
    return None


def _ctl_set_speed(cmd: str, value: Any) -> int:
    return _ctl_int(value, 1, 20, cmd)


def _ctl_back_lines(cmd: str, value: Any) -> int:
    return 3 if value is None else _ctl_int(value, 1, 50, cmd)


def _ctl_scroll(cmd: str, value: Any) -> int:
    return _ctl_int(value, -5, 5, cmd, nonzero=True)


def _ctl_font(cmd: str, value: Any) -> int:
    if not _is_int(value) or value not in (1, -1):
        raise ProtocolError("'value' de 'font' debe ser +1 o -1.")
    return value


def _ctl_mode(cmd: str, value: Any) -> str:
    if not isinstance(value, str) or value not in CONTROL_MODES:
        raise ProtocolError("'value' de 'mode' debe ser manual, asistido, voz o siguiente.")
    return value


def _ctl_view(cmd: str, value: Any) -> str:
    if not isinstance(value, str) or value not in CONTROL_VIEWS:
        raise ProtocolError("'value' de 'view' debe ser texto, referencias o siguiente.")
    return value


def _ctl_backdrop(cmd: str, value: Any) -> int:
    return _ctl_int(value, -100, 100, cmd, nonzero=True)


def _ctl_countdown(cmd: str, value: Any) -> int:
    return 3 if value is None else _ctl_int(value, 0, 30, cmd)


_SCRIPT_NAME_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,120}$')


def _ctl_open_script(cmd: str, value: Any) -> str:
    if not isinstance(value, str) or not _SCRIPT_NAME_RE.match(value) or not value.strip(". "):
        raise ProtocolError("'value' de 'open_script' debe ser el nombre de un archivo de guiones/.")
    return value


_CONTROL_PARSERS = {
    "play_pause": _ctl_none, "faster": _ctl_none, "slower": _ctl_none,
    "set_speed": _ctl_set_speed,
    "prev_paragraph": _ctl_none, "back_lines": _ctl_back_lines, "next_section": _ctl_none,
    "top": _ctl_none, "scroll": _ctl_scroll,
    "font": _ctl_font, "mirror": _ctl_none,
    "mode": _ctl_mode,
    "backdrop": _ctl_backdrop, "band": _ctl_none, "halo": _ctl_none,
    "lock": _ctl_none, "topmost": _ctl_none, "countdown": _ctl_countdown,
    "open_script": _ctl_open_script,
    "view": _ctl_view, "ref_zoom": _ctl_none,   # hito 85 (R5)
}
CONTROL_CMDS = frozenset(_CONTROL_PARSERS)


def parse_control_message(raw: str | bytes) -> ControlCmd:
    """Valida una orden de /control: `{"v":1,"cmd":...,"value":...}`. Lanza ProtocolError."""
    if not isinstance(raw, str):
        raise ProtocolError("Solo se aceptan mensajes de texto con JSON.")
    if len(raw) > CONTROL_MAX_BYTES:
        raise ProtocolError("La orden supera el máximo de 4 KB.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProtocolError("La orden no es JSON válido.") from None
    if not isinstance(data, dict):
        raise ProtocolError("La orden debe ser un objeto JSON.")
    v = data.get("v")
    if not _is_int(v) or v != CONTROL_VERSION:
        raise ProtocolError('Versión de control no soportada: se espera "v": 1.')
    cmd = data.get("cmd")
    if not isinstance(cmd, str) or cmd not in _CONTROL_PARSERS:
        shown = cmd[:40] if isinstance(cmd, str) else cmd
        raise ProtocolError(f"Orden desconocida: {shown!r}.")
    return ControlCmd(cmd=cmd, value=_CONTROL_PARSERS[cmd](cmd, data.get("value")))


def control_forward_msg(cmd: ControlCmd) -> dict:
    """Servidor -> visor (/ws): una orden de un control para que la ejecute."""
    return {"type": "control", "cmd": cmd.cmd, "value": cmd.value}


def control_error_msg(message: str, code: str = "BAD_REQUEST") -> dict:
    return {"v": CONTROL_VERSION, "type": "status", "state": "error", "code": code,
            "message": message}


# Campos del estado (app -> control), en el orden del contrato.
ESTADO_FIELDS = ("corriendo", "modo", "velocidad", "ppm", "progreso", "transcurrido_s",
                 "restante_s", "seccion", "seccion_idx", "secciones", "letra", "espejo",
                 "fondo_pct", "banda", "halo", "bloqueado", "adelante", "guion", "cuenta",
                 "vista", "ref_actual", "ref_ampliada")   # las tres últimas: hito 85 (R5)
_ESTADO_MODES = frozenset({"manual", "asistido", "voz"})
_ESTADO_VISTAS = frozenset({"texto", "referencias"})


def _num(value: Any, lo: float, hi: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < lo or (hi is not None and value > hi):
        return None
    return value


def _int_or_none(value: Any, lo: int, hi: int | None = None) -> int | None:
    v = _num(value, lo, hi)
    return None if v is None or v != int(v) else int(v)


def _round(value: float | None, nd: int) -> float | None:
    return None if value is None else round(value, nd)


def _text(value: Any, limit: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    return _SURROGATE_RE.sub("�", value[:limit])


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def sanitize_estado(raw: dict) -> dict:
    """Toma del visor solo los campos del contrato; un valor inválido queda en None (sin dato)."""
    modo = raw.get("modo")
    vista = raw.get("vista")
    return {
        "corriendo": _flag(raw.get("corriendo")),
        "modo": modo if isinstance(modo, str) and modo in _ESTADO_MODES else None,
        "velocidad": _int_or_none(raw.get("velocidad"), 1, 20),
        "ppm": _round(_num(raw.get("ppm"), 0, 2000), 1),
        "progreso": _round(_num(raw.get("progreso"), 0, 1), 4),
        "transcurrido_s": _round(_num(raw.get("transcurrido_s"), 0), 1),
        "restante_s": _round(_num(raw.get("restante_s"), 0), 1),
        "seccion": _text(raw.get("seccion")),
        "seccion_idx": _int_or_none(raw.get("seccion_idx"), 0),
        "secciones": _int_or_none(raw.get("secciones"), 0),
        "letra": _int_or_none(raw.get("letra"), 1, 100),
        "espejo": _flag(raw.get("espejo")),
        "fondo_pct": _int_or_none(raw.get("fondo_pct"), 0, 100),
        "banda": _flag(raw.get("banda")),
        "halo": _flag(raw.get("halo")),
        "bloqueado": _flag(raw.get("bloqueado")),
        "adelante": _flag(raw.get("adelante")),
        "guion": _text(raw.get("guion"), 120),
        "cuenta": _int_or_none(raw.get("cuenta"), 0, 30),
        "vista": vista if isinstance(vista, str) and vista in _ESTADO_VISTAS else None,
        "ref_actual": _text(raw.get("ref_actual")),
        "ref_ampliada": _flag(raw.get("ref_ampliada")),
    }


def estado_control_msg(fields: dict | None, *, conectado: bool) -> dict:
    """App -> control. Sin visor (`conectado: false`) los campos sin dato van en null."""
    msg: dict = {"v": CONTROL_VERSION, "type": "estado", "conectado": bool(conectado)}
    base = fields or {}
    for key in ESTADO_FIELDS:
        msg[key] = base.get(key)
    if not conectado:
        msg["corriendo"] = False
    return msg
