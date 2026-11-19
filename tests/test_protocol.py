"""Protocolo WebSocket v2 de punta a punta con STT y audio falsos.

Usa `TestClient` de Starlette con `create_app(Settings(...))` y fábricas que
inyectan `FakeSTTEngine`, `FakeVadGate` y `FakeAudioSource`: sin GPU,
micrófono ni red. Ninguna espera supera los 5 s. Con el bucle de la Fase 2,
la primera inferencia tras cada start o reposicionamiento espera a tener
`MIN_WINDOW_S` (1 s) de audio nuevo.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
import time
import wave
from collections.abc import Callable, Iterator

import numpy as np
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from backend import server as server_mod
from backend import voice as voice_mod
from backend.config import Settings
from backend.fakes import FakeAudioSource, FakeModelError, FakeSTTEngine, FakeVadGate
from backend.protocol import (
    MAX_MESSAGE_BYTES, Hello, LoadScript, ProtocolError, SetPosition, Start, encode, level_msg,
    parse_client_message, scroll_msg, status_msg, transcript_msg,
)
from backend.server import Connection, Outbox, Session, create_app
from tools.script_parser import MAX_SCRIPT_CHARS, parse_script

TIMEOUT = 5.0


def _word(i: int) -> str:
    """Palabra artificial única; dos cualesquiera difieren en 2 letras o más
    (el matcher no las confunde por coincidencia difusa)."""
    digits = []
    for _ in range(3):
        i, d = divmod(i, 26)
        digits.append(d)
    return "".join(chr(ord("a") + d) * 2 for d in reversed(digits))


WORDS = [_word(i) for i in range(200)]
SCRIPT = " ".join(WORDS[:100]) + "\n\n[Mostrar slide 2]\n" + " ".join(WORDS[100:])


# ---------------------------------------------------------------- utilidades

class Ws:
    """Cliente de prueba con recepción con timeout.

    `WebSocketTestSession.receive()` de Starlette 0.38.6 espera sin límite,
    así que se lee su cola interna con timeout.
    """

    def __init__(self, session) -> None:
        self.ws = session
        self.raw: list[str] = []  # texto de cada mensaje recibido, tal cual

    def send(self, **msg) -> None:
        self.ws.send_text(json.dumps(msg))

    def _get(self, timeout: float) -> dict:
        try:
            message = self.ws._send_queue.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(message, BaseException):
            raise message
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        self.raw.append(message["text"])
        return json.loads(message["text"])

    def next(self, timeout: float = TIMEOUT) -> dict:
        """Siguiente mensaje que no sea `level`."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                msg = self._get(deadline - time.monotonic())
            except TimeoutError:
                pytest.fail(f"No llegó ningún mensaje en {timeout} s.")
            if msg["type"] != "level":
                return msg

    def wait_for(self, pred: Callable[[dict], bool],
                 timeout: float = TIMEOUT) -> tuple[dict, list[dict]]:
        """Primer mensaje que cumple `pred` y los que llegaron antes (sin `level`)."""
        deadline = time.monotonic() + timeout
        skipped: list[dict] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(f"No llegó el mensaje esperado; llegaron: {skipped}")
            msg = self.next(remaining)
            if pred(msg):
                return msg, skipped
            skipped.append(msg)

    def wait_type(self, type_: str, **fields) -> dict:
        msg, _ = self.wait_for(
            lambda m: m["type"] == type_ and all(m.get(k) == v for k, v in fields.items()))
        return msg

    def drain(self, duration: float) -> list[dict]:
        """Todos los mensajes (sin `level`) que lleguen durante `duration` segundos."""
        deadline = time.monotonic() + duration
        out: list[dict] = []
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                msg = self._get(remaining)
            except TimeoutError:
                break
            if msg["type"] != "level":
                out.append(msg)
        return out

    def wait_close(self, timeout: float = TIMEOUT) -> tuple[int, str]:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                self._get(remaining)
            except WebSocketDisconnect as exc:
                return exc.code, exc.reason
            except TimeoutError:
                break
        pytest.fail("La conexión no se cerró.")

    # atajos del protocolo -------------------------------------------------

    def hello(self) -> dict:
        self.send(action="hello", client="pytest")
        state = self.next()
        assert state["type"] == "state"
        return state

    def load(self, text: str = SCRIPT, seq: int = 1) -> tuple[dict, dict]:
        self.send(action="load_script", text=text, seq=seq)
        loaded = self.wait_type("script_loaded")
        state = self.next()
        assert state["type"] == "state"
        return loaded, state

    def start(self, idx: int, seq: int) -> dict:
        self.send(action="start", idx=idx, seq=seq)
        return self.start_reply()

    def start_reply(self) -> dict:
        """`status: listening` y el eco de un start ya enviado; devuelve el eco."""
        status = self.next()
        assert status == {"type": "status", "state": "listening", "code": None,
                          "message": status["message"]}, status
        echo = self.next()
        assert echo["type"] == "scroll", echo
        return echo


class Harness:
    def __init__(self, app, client: TestClient, stt: FakeSTTEngine,
                 audio: FakeAudioSource | None, vad: FakeVadGate | None = None) -> None:
        self.app = app
        self.client = client
        self.stt = stt
        self.audio = audio
        self.vad = vad
        self.session = app.state.session

    @contextlib.contextmanager
    def connect(self) -> Iterator[Ws]:
        with self.client.websocket_connect("/ws") as ws:
            yield Ws(ws)

    def wait(self, pred: Callable[[], bool], what: str, timeout: float = TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                pytest.fail(f"Se agotó la espera: {what}")
            time.sleep(0.01)


def _wait_model_loaded(session: Session) -> None:
    deadline = time.monotonic() + TIMEOUT
    while session.model_state == "loading":
        if time.monotonic() > deadline:
            pytest.fail("Se agotó la espera: carga del modelo falso")
        time.sleep(0.01)


@pytest.fixture
def make() -> Iterator[Callable[..., Harness]]:
    """Fábrica de app + TestClient con falsos; libera todo al terminar."""
    stack = contextlib.ExitStack()
    gates: list[threading.Event] = []
    stts: list[FakeSTTEngine] = []

    def factory(*, stt: FakeSTTEngine | None = None, audio: FakeAudioSource | None = None,
                audio_factory: Callable | None = None, vad: FakeVadGate | None = None,
                vad_factory: Callable | None = None, wait_model: bool = True,
                **overrides) -> Harness:
        settings = Settings(fake_stt=True, step_ms=20, **overrides)
        stt = stt if stt is not None else FakeSTTEngine(device=settings.device)
        if audio_factory is None:
            audio = audio if audio is not None else FakeAudioSource(block_s=0.02)
            audio_factory = lambda s: audio  # noqa: E731
        if vad_factory is None:
            vad = vad if vad is not None else FakeVadGate()
            vad_factory = lambda s: vad  # noqa: E731
        stts.append(stt)
        if stt.load_gate is not None:
            gates.append(stt.load_gate)
        app = create_app(settings, stt_factory=lambda s: stt, audio_factory=audio_factory,
                         vad_factory=vad_factory)
        h = Harness(app, stack.enter_context(TestClient(app)), stt, audio, vad)
        if wait_model:
            h.wait(lambda: h.session.model_state != "loading", "carga del modelo falso")
        return h

    try:
        yield factory
    finally:
        for gate in gates:  # nada queda bloqueado al cerrar
            gate.set()
        for stt in stts:
            stt.release()
        stack.close()


# ---------------------------------------------------------------- conexión y guion

def test_hello_devuelve_state(make) -> None:
    h = make()
    with h.connect() as c:
        state = c.hello()
        assert state == {"type": "state", "protocol": 2, "model": "ready", "model_name": "medium",
                         "device": "cuda", "listening": False, "script_id": None,
                         "total_tokens": 0, "current_idx": 0, "epoch": 0, "debug": False}
        assert c.drain(0.2) == []  # sin guion no hay script_loaded


def test_load_script_responde_script_loaded_y_state(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        loaded, state = c.load(seq=1)
        assert loaded["epoch"] == 1 and loaded["total_tokens"] == 200
        assert len(loaded["script_id"]) == 32
        assert loaded["text"] == SCRIPT
        assert loaded["segments"] == parse_script(SCRIPT).segments
        assert {"type": "directive", "text": "[Mostrar slide 2]", "at_idx": 100} in loaded["segments"]
        assert state["script_id"] == loaded["script_id"]
        assert (state["total_tokens"], state["current_idx"], state["epoch"]) == (200, 0, 1)
        assert state["listening"] is False


def test_get_index_y_estaticos(make) -> None:
    h = make()
    r = h.client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert h.client.get("/static/app.js").status_code == 200


# ---------------------------------------------------------------- start y épocas

def test_start_con_idx_y_la_voz_sigue_desde_ahi(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        echo = c.start(idx=120, seq=2)
        assert echo == {"type": "scroll", "current_idx": 120, "epoch": 2, "confidence": 1.0,
                        "matched_word": None, "stt_ms": 0, "lag_ms": 0, "resync": False}
        # Lo que se lee a partir de 120 avanza desde 120 (no vuelve a una posición vieja).
        h.stt.push(" ".join(WORDS[120:124]).upper() + ".")
        scroll = c.wait_type("scroll")
        assert scroll["epoch"] == 2
        assert 120 < scroll["current_idx"] <= 124
        assert scroll["matched_word"] in WORDS[120:124]
        assert scroll["stt_ms"] >= 0 and scroll["lag_ms"] >= 0
        assert h.session.matcher.current_idx == scroll["current_idx"]
        # Fase 2: ventana de al menos 1 s (MIN_WINDOW_S) y como mucho window_s (3 s).
        assert int(voice_mod.MIN_WINDOW_S * 16000) <= h.stt.last_samples <= 3 * 16000
        assert set(h.stt.prompts) == {None}  # sin TP_PROMPT_CONTEXT no hay initial_prompt


@pytest.mark.parametrize("case", ["silencio", "vad_sin_voz"])
def test_sin_voz_no_se_transcribe(make, case: str) -> None:
    if case == "silencio":  # el VAD falso por defecto no ve voz en el silencio
        h = make(audio=FakeAudioSource(amplitude=0.0, block_s=0.02))
    else:  # hay señal, pero el VAD dice que no es voz
        h = make(vad=FakeVadGate(voiced=False))
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        warmup = h.vad.calls  # la carga del modelo ya consultó el VAD una vez
        assert warmup == 1
        c.start(idx=0, seq=2)
        h.wait(lambda: h.vad.calls >= warmup + 3, "el bucle consultó el VAD")
        assert h.vad.last_samples >= 16000 and h.vad.last_tail_s == 1.0
        assert h.stt.calls == 0
        level = c.ws._send_queue.get(timeout=TIMEOUT)  # el medidor sí informa
        rms_db = json.loads(level["text"])["rms_db"]
        assert rms_db == -120.0 if case == "silencio" else rms_db > -40.0


def test_fabricas_por_defecto_con_fake_stt() -> None:
    app = create_app(Settings(fake_stt=True, step_ms=20))
    with TestClient(app) as client:
        # Se conecta con el modelo ya listo: si no, sus avisos llegarían antes del state.
        _wait_model_loaded(app.state.session)
        with client.websocket_connect("/ws") as ws:
            c = Ws(ws)
            assert c.hello()["model"] == "ready"
            c.load(seq=1)
            assert c.start(idx=3, seq=2)["current_idx"] == 3
            assert isinstance(app.state.session.voice._audio, FakeAudioSource)
            assert isinstance(app.state.session.stt, FakeSTTEngine)
            assert isinstance(app.state.session.vad, FakeVadGate)
            c.send(action="pause")
            c.wait_type("status", state="paused")


def test_start_escuchando_solo_reposiciona(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        echo = c.start(idx=50, seq=3)
        assert (echo["current_idx"], echo["epoch"]) == (50, 3)
        assert h.audio.start_calls == 1
        assert h.session.listening


def test_resultado_de_una_epoca_vieja_se_descarta(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        # Inferencia lenta de audio anterior al salto.
        h.stt.hold()
        h.stt.push(" ".join(WORDS[3:6]))
        assert h.stt.wait_holding(), "la inferencia no quedó retenida"
        calls = h.stt.calls
        c.send(action="set_position", idx=3, seq=3)
        echo, _ = c.wait_for(lambda m: m["type"] == "scroll")
        assert (echo["current_idx"], echo["epoch"], echo["confidence"]) == (3, 3, 1.0)
        h.stt.release()
        assert h.stt.wait_calls(calls + 1)  # el bucle ya procesó el resultado viejo
        assert h.session.matcher.current_idx == 3, "se aplicó un resultado de la época anterior"
        # Lo nuevo sí avanza, y todo scroll lleva la última época.
        h.stt.push(" ".join(WORDS[3:7]))
        fresh, skipped = c.wait_for(lambda m: m["type"] == "scroll")
        assert fresh["epoch"] == 3 and fresh["current_idx"] > 3
        assert not [m for m in skipped if m["type"] == "scroll"]


def test_nunca_dos_inferencias_a_la_vez(make, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[object] = []  # resultado de cada intento de inferencia que terminó
    real_infer = voice_mod.VoiceLoop._infer

    def spy(self, *args):
        result = real_infer(self, *args)
        attempts.append(result)
        return result

    monkeypatch.setattr(voice_mod.VoiceLoop, "_infer", spy)
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        h.stt.hold()
        h.stt.push(WORDS[0])
        assert h.stt.wait_holding()
        calls = h.stt.calls
        done_before = len(attempts)
        c.send(action="pause")
        c.wait_type("status", state="paused")
        c.start(idx=0, seq=3)  # bucle nuevo con la inferencia anterior todavía en curso
        # El bucle nuevo solo intenta transcribir cuando junta MIN_WINDOW_S de
        # audio nuevo: se espera a ese intento (no basta con dormir un rato).
        h.wait(lambda: len(attempts) > done_before, "intento de inferencia del bucle nuevo")
        assert attempts[done_before] is None, "el bucle nuevo no vio el motor ocupado"
        assert h.stt.calls == calls, "se lanzó una segunda inferencia en paralelo"
        h.stt.release()
        assert h.stt.wait_calls(calls + 1)


def test_set_position_responde_eco_y_adopta_la_epoca(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.send(action="set_position", idx=200, seq=4)
        echo = c.next()
        assert (echo["type"], echo["current_idx"], echo["epoch"]) == ("scroll", 200, 4)
        c.send(action="set_position", idx=7)  # sin seq: conserva la época
        echo = c.next()
        assert (echo["current_idx"], echo["epoch"]) == (7, 4)


# ---------------------------------------------------------------- conexión única

def test_segunda_conexion_reemplaza_a_la_primera(make) -> None:
    h = make()
    with h.connect() as c1:
        c1.hello()
        loaded, _ = c1.load(seq=1)
        c1.start(idx=10, seq=2)
        with h.connect() as c2:
            assert c1.wait_close() == (4000, "reemplazada")
            h.wait(lambda: not h.session.listening, "escucha detenida al reemplazar")
            c1.send(action="start", idx=0, seq=9)  # la conexión vieja ya no manda
            state = c2.hello()
            assert state["listening"] is False
            assert (state["script_id"], state["current_idx"], state["epoch"]) == (
                loaded["script_id"], 10, 2)
            assert c2.next()["type"] == "script_loaded"
            assert not h.session.listening


def _spy_done(monkeypatch: pytest.MonkeyPatch, method: str) -> threading.Event:
    """Evento que se activa cuando termina cada llamada a `Session.<method>`."""
    done = threading.Event()
    real = getattr(Session, method)

    async def spy(self, *args, **kwargs):
        try:
            return await real(self, *args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(Session, method, spy)
    return done


def test_start_en_curso_de_una_conexion_reemplazada_se_descarta(
        make, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    audio = FakeAudioSource(block_s=0.02)

    def slow_factory(settings):  # abrir el micrófono tarda
        entered.set()
        release.wait(TIMEOUT)
        return audio

    done = _spy_done(monkeypatch, "_on_start")
    h = make(audio_factory=slow_factory)
    try:
        with h.connect() as c1:
            c1.hello()
            c1.load(seq=1)
            c1.send(action="start", idx=4, seq=2)
            assert entered.wait(TIMEOUT), "el start no llegó a abrir la fuente"
            with h.connect() as c2:
                assert c1.wait_close() == (4000, "reemplazada")
                assert c2.hello()["listening"] is False
                release.set()
                assert done.wait(TIMEOUT)
                # El start de la conexión vieja abrió la fuente, pero no se instala.
                assert not h.session.listening
                h.wait(lambda: audio.stop_calls >= audio.start_calls == 1, "fuente descartada")
                assert not audio.running
                c2.send(action="hello")
                state = c2.wait_type("state")
                assert (state["listening"], state["current_idx"], state["epoch"]) == (False, 0, 1)
                # La conexión nueva sí puede escuchar después.
                c2.send(action="start", idx=0, seq=5)
                c2.wait_type("status", state="listening")
                assert h.session.listening and audio.running
    finally:
        release.set()


def test_load_script_en_curso_de_una_conexion_reemplazada_se_descarta(
        make, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()
    real_parse = server_mod.parse_script

    def slow_parse(text):  # guion enorme
        entered.set()
        release.wait(TIMEOUT)
        return real_parse(text)

    monkeypatch.setattr(server_mod, "parse_script", slow_parse)
    done = _spy_done(monkeypatch, "_on_load_script")
    h = make()
    try:
        with h.connect() as c1:
            c1.hello()
            c1.send(action="load_script", text=SCRIPT, seq=1)
            assert entered.wait(TIMEOUT)
            with h.connect() as c2:
                assert c1.wait_close() == (4000, "reemplazada")
                release.set()
                assert done.wait(TIMEOUT)
                assert h.session.script is None, "se aplicó el guion de la conexión reemplazada"
                state = c2.hello()
                assert (state["script_id"], state["epoch"]) == (None, 0)
                assert c2.drain(0.1) == []  # sin script_loaded
    finally:
        release.set()


def test_reconexion_recupera_guion_y_posicion(make) -> None:
    h = make()
    with h.connect() as c1:
        c1.hello()
        loaded, _ = c1.load(seq=1)
        c1.send(action="set_position", idx=37, seq=2)
        assert c1.next()["current_idx"] == 37
        c1.start(idx=37, seq=3)
        assert h.session.listening
    h.wait(lambda: not h.session.listening, "escucha detenida al desconectarse el cliente")
    with h.connect() as c2:
        state = c2.hello()
        assert (state["script_id"], state["current_idx"], state["epoch"]) == (
            loaded["script_id"], 37, 3)
        assert state["listening"] is False
        again = c2.next()
        assert again["type"] == "script_loaded"
        assert again["script_id"] == loaded["script_id"]
        assert again["segments"] == loaded["segments"] and again["epoch"] == 3


# ---------------------------------------------------------------- validación

def test_payload_invalido_responde_bad_request_sin_cerrar(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        bad = [
            "esto no es json",
            "[1, 2, 3]",
            "null",
            json.dumps({"sin": "action"}),
            json.dumps({"action": 5}),
            json.dumps({"action": "volar"}),
            json.dumps({"action": "set_position"}),
            json.dumps({"action": "set_position", "idx": "3"}),
            json.dumps({"action": "set_position", "idx": True}),
            json.dumps({"action": "set_position", "idx": 2.0}),
            json.dumps({"action": "set_position", "idx": -1}),
            json.dumps({"action": "set_position", "idx": 201}),
            json.dumps({"action": "start", "idx": 999, "seq": 2}),
            json.dumps({"action": "start", "idx": 0, "seq": -1}),
            json.dumps({"action": "start", "idx": 0, "seq": False}),
            json.dumps({"action": "load_script", "text": "   "}),
            json.dumps({"action": "load_script", "text": 42}),
            json.dumps({"action": "load_script", "text": "x" * (MAX_SCRIPT_CHARS + 1)}),
            json.dumps({"action": "set_input_device", "device": 3}),
            json.dumps({"action": "mic_test", "on": True}),
            "x" * (MAX_MESSAGE_BYTES + 1),
        ]
        for raw in bad:
            c.ws.send_text(raw)
            msg = c.next()
            assert msg["type"] == "status" and msg["state"] == "error", (raw[:60], msg)
            assert msg["code"] == "BAD_REQUEST", (raw[:60], msg)
            assert msg["message"]
        c.ws.send_bytes(b'{"action": "hello"}')
        assert c.next()["code"] == "BAD_REQUEST"
        # La conexión sigue viva y el estado no cambió.
        c.send(action="hello", extra="los campos desconocidos se ignoran")
        state = c.next()
        assert state["type"] == "state"
        assert (state["current_idx"], state["epoch"], state["listening"]) == (0, 1, False)
        assert h.session.conn is not None


def test_sin_guion_start_y_set_position_responden_no_script(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.send(action="start", idx=120, seq=1)
        err = c.next()
        assert (err["type"], err["state"], err["code"]) == ("status", "error", "NO_SCRIPT")
        state = c.next()
        assert state["type"] == "state" and state["listening"] is False
        c.send(action="set_position", idx=0, seq=2)
        assert c.next()["code"] == "NO_SCRIPT"
        assert h.audio.start_calls == 0


# ---------------------------------------------------------------- transcript y debug

@pytest.mark.parametrize("debug", [False, True])
def test_transcript_solo_con_debug(make, debug: bool) -> None:
    h = make(debug=debug)
    with h.connect() as c:
        assert c.hello()["debug"] is debug
        c.load(seq=1)
        c.start(idx=0, seq=2)
        text = " ".join(WORDS[0:4])
        h.stt.push(text)
        scroll, before = c.wait_for(lambda m: m["type"] == "scroll")
        c.send(action="pause")
        _, after = c.wait_for(lambda m: m["type"] == "status" and m["state"] == "paused")
        transcripts = [m for m in before + after if m["type"] == "transcript"]
        if debug:
            assert transcripts == [{"type": "transcript", "text": text, "is_final": False}]
        else:
            assert transcripts == []
        assert scroll["current_idx"] > 0


# ---------------------------------------------------------------- errores

def test_mic_error_responde_error_y_queda_en_pausa(make) -> None:
    audio = FakeAudioSource(start_error=OSError("dispositivo no disponible"), block_s=0.02)
    h = make(audio=audio)
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.send(action="start", idx=40, seq=2)
        err = c.next()
        assert (err["type"], err["state"], err["code"]) == ("status", "error", "MIC_ERROR")
        assert "dispositivo no disponible" in err["message"]
        state = c.next()
        assert state["type"] == "state" and state["listening"] is False
        # Atómico: la posición y la época no cambiaron.
        assert (state["current_idx"], state["epoch"]) == (0, 1)
        assert not h.session.listening
        audio.start_error = None  # el micrófono vuelve
        assert c.start(idx=40, seq=3)["current_idx"] == 40


def test_mic_lost_detiene_la_escucha(make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_mod, "MIC_TIMEOUT_S", 0.3)
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        h.audio.stall()
        err = c.wait_type("status", state="error")
        assert err["code"] == "MIC_LOST"
        state = c.next()
        assert state["type"] == "state" and state["listening"] is False
        assert not h.session.listening


def test_error_interno_del_bucle_no_tumba_el_servidor(make) -> None:
    stt = FakeSTTEngine(device="cuda", transcribe_error=RuntimeError("fallo simulado"))
    h = make(stt=stt)
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        err = c.wait_type("status", state="error")
        assert err["code"] == "INTERNAL"
        assert c.next()["listening"] is False
        assert c.hello()["type"] == "state"  # el servidor sigue respondiendo
    assert h.client.get("/").status_code == 200


def test_error_del_stt_con_codigo_llega_con_su_codigo(make) -> None:
    # STTError de faster-whisper (aquí, su doble): el usuario ve la causa.
    error = FakeModelError("Falló la transcripción: RuntimeError: CUDA failed with error "
                           "out of memory", code="MODEL_ERROR")
    h = make(stt=FakeSTTEngine(device="cuda", transcribe_error=error))
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        err = c.wait_type("status", state="error")
        assert err["code"] == "MODEL_ERROR"
        assert "out of memory" in err["message"] and "pausó la escucha" in err["message"]
        state = c.next()
        assert state["listening"] is False and state["model"] == "ready"


class SlowStopAudio(FakeAudioSource):
    """Fuente cuyo stop() espera a `stop_gate`, como un driver que tarda en cerrar
    un micrófono que acaba de desaparecer."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stop_gate = threading.Event()
        self.stop_entered = threading.Event()
        self.stop_done = threading.Event()

    def stop(self) -> None:
        self.stop_entered.set()
        self.stop_gate.wait(TIMEOUT)
        super().stop()
        self.stop_done.set()


def test_mic_lost_se_informa_antes_de_detener_la_fuente(
        make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_mod, "MIC_TIMEOUT_S", 0.3)
    audio = SlowStopAudio(block_s=0.02)
    h = make(audio=audio)
    try:
        with h.connect() as c:
            c.hello()
            c.load(seq=1)
            c.start(idx=0, seq=2)
            audio.stall()
            err = c.wait_type("status", state="error")
            assert err["code"] == "MIC_LOST"
            assert c.next()["listening"] is False
            assert not audio.stop_done.is_set(), "el aviso esperó a que se cerrara la fuente"
            # Pausa y reinicio enseguida: el start espera a que la fuente se
            # detenga y ningún mensaje del error viejo llega después de él.
            c.send(action="pause")
            c.wait_type("status", state="paused")
            audio.resume()
            c.send(action="start", idx=5, seq=3)
            assert c.drain(0.2) == []  # sigue esperando a la fuente
            audio.stop_gate.set()
            assert c.start_reply()["current_idx"] == 5
            assert audio.stop_done.is_set() and audio.start_calls == 2
            assert [m for m in c.drain(0.3) if m["type"] in ("status", "state")] == []
            assert h.session.listening
    finally:
        audio.stop_gate.set()


def test_fuente_atascada_no_bloquea_la_pausa_y_se_reemplaza(
        make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_mod, "AUDIO_STOP_TIMEOUT_S", 0.2)
    sources: list[SlowStopAudio] = []

    def factory(settings) -> SlowStopAudio:
        sources.append(SlowStopAudio(block_s=0.02))
        return sources[-1]

    h = make(audio_factory=factory)
    try:
        with h.connect() as c:
            c.hello()
            c.load(seq=1)
            c.start(idx=0, seq=2)
            t0 = time.monotonic()
            c.send(action="pause")
            c.wait_type("status", state="paused")
            assert time.monotonic() - t0 < 2.0, "la pausa esperó a la fuente atascada"
            assert sources[0].stop_entered.is_set() and not sources[0].stop_done.is_set()
            # El próximo start no reutiliza la fuente atascada.
            c.start(idx=0, seq=3)
            assert len(sources) == 2 and h.session.voice.audio is sources[1]
    finally:
        for src in sources:
            src.stop_gate.set()


def test_la_fuente_de_audio_se_reutiliza_entre_starts(make) -> None:
    audio = FakeAudioSource(block_s=0.02)
    created: list[FakeAudioSource] = []

    def factory(settings) -> FakeAudioSource:
        created.append(audio)
        return audio

    h = make(audio_factory=factory)
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        c.send(action="pause")
        c.wait_type("status", state="paused")
        c.start(idx=3, seq=3)
        assert len(created) == 1 and audio.start_calls == 2
        assert h.session.voice.audio is audio


def _write_tone_wav(path, seconds: float) -> str:
    n = int(seconds * 16000)
    tone = 0.3 * np.sin(2 * np.pi * 220.0 * np.arange(n) / 16000)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((tone * 32767).astype("<i2").tobytes())
    return str(path)


def test_audio_file_reanuda_donde_quedo_tras_pausa(tmp_path) -> None:
    # TP_AUDIO_FILE con la fábrica por defecto: pausar y reanudar no vuelve al
    # principio del WAV (el matcher conserva su posición).
    wav = _write_tone_wav(tmp_path / "demo.wav", 10.0)
    stt = FakeSTTEngine()
    app = create_app(Settings(audio_file=wav, step_ms=20), stt_factory=lambda s: stt,
                     vad_factory=lambda s: FakeVadGate())
    session = app.state.session
    with TestClient(app) as client:
        _wait_model_loaded(session)
        with client.websocket_connect("/ws") as ws:
            c = Ws(ws)
            c.hello()
            c.load(seq=1)
            c.start(idx=0, seq=2)
            src = session.voice.audio
            deadline = time.monotonic() + TIMEOUT
            while src.position_s < 0.5 and time.monotonic() < deadline:
                time.sleep(0.01)
            c.send(action="pause")
            c.wait_type("status", state="paused")
            paused_at = src.position_s
            assert paused_at >= 0.5
            c.start(idx=0, seq=3)
            assert session.voice.audio is src
            assert src.position_s >= paused_at  # sigue, no vuelve a 0
            c.send(action="pause")
            c.wait_type("status", state="paused")


def test_model_not_ready_mientras_carga(make) -> None:
    gate = threading.Event()
    h = make(stt=FakeSTTEngine(device="cuda", load_gate=gate), wait_model=False)
    with h.connect() as c:
        assert c.hello()["model"] == "loading"
        assert c.next() == {"type": "status", "state": "loading", "code": None,
                            "message": "Cargando el modelo medium…"}
        c.load(seq=1)
        c.send(action="start", idx=0, seq=2)
        err = c.next()
        assert (err["state"], err["code"]) == ("error", "MODEL_NOT_READY")
        assert c.next()["listening"] is False
        gate.set()
        ready = c.wait_type("status", state="ready")
        assert ready["code"] is None
        state = c.next()
        assert (state["type"], state["model"], state["device"]) == ("state", "ready", "cuda")
        assert c.start(idx=0, seq=3)["epoch"] == 3


def test_modelo_faltante_informa_model_missing(make) -> None:
    gate = threading.Event()
    stt = FakeSTTEngine(device="cuda", load_gate=gate,
                        load_error=FakeModelError("Falta el modelo medium.", code="MODEL_MISSING"))
    h = make(stt=stt, wait_model=False)
    with h.connect() as c:
        c.hello()
        assert c.next()["state"] == "loading"
        gate.set()
        err = c.wait_type("status", state="error")
        assert (err["code"], err["message"]) == ("MODEL_MISSING", "Falta el modelo medium.")
        assert c.next()["model"] == "error"
        c.load(seq=1)
        c.send(action="start", idx=0, seq=2)
        assert c.next()["code"] == "MODEL_NOT_READY"
        assert c.next()["type"] == "state"
        # Un cliente que saluda después también ve el motivo.
        assert c.hello()["model"] == "error"
        assert c.wait_type("status", state="error")["code"] == "MODEL_MISSING"


def test_fallback_a_cpu_avisa_cuda_fallback(make) -> None:
    gate = threading.Event()
    h = make(stt=FakeSTTEngine(device="cuda", cuda_fallback=True, load_gate=gate),
             wait_model=False)
    with h.connect() as c:
        c.hello()
        c.next()  # status loading
        gate.set()
        assert c.next()["state"] == "ready"
        warn = c.next()
        assert (warn["state"], warn["code"]) == ("warning", "CUDA_FALLBACK")
        state = c.next()
        assert (state["model"], state["device"]) == ("ready", "cpu")


def test_vad_uno_por_sesion_y_cargado_con_el_modelo(make) -> None:
    created: list[FakeVadGate] = []

    def vad_factory(settings: Settings) -> FakeVadGate:
        created.append(FakeVadGate(threshold=settings.vad_threshold))
        return created[-1]

    h = make(vad_factory=vad_factory, vad_threshold=0.35)
    # Se crea y se precalienta en el hilo de carga, antes de quedar listo:
    # el primer ciclo no paga la carga de Silero.
    assert len(created) == 1
    vad = created[0]
    assert vad.calls == 1 and vad.threads == {"stt-load"} and vad.threshold == 0.35
    assert h.session.vad is vad
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=0, seq=2)
        c.send(action="pause")
        c.wait_type("status", state="paused")
        c.start(idx=0, seq=3)
        h.wait(lambda: vad.calls > 1, "el bucle consultó el VAD de la sesión")
    assert len(created) == 1


def test_vad_que_no_carga_informa_model_error(make) -> None:
    def broken_vad(settings: Settings) -> FakeVadGate:
        raise RuntimeError("onnxruntime no disponible")

    h = make(vad_factory=broken_vad)
    assert h.session.model_state == "error"
    with h.connect() as c:
        assert c.hello()["model"] == "error"
        err = c.wait_type("status", state="error")
        assert err["code"] == "MODEL_ERROR"
        assert "detector de voz" in err["message"] and "onnxruntime" in err["message"]
        c.load(seq=1)
        c.send(action="start", idx=0, seq=2)
        assert c.next()["code"] == "MODEL_NOT_READY"


def test_prompt_context_pasa_el_guion_anterior(make) -> None:
    h = make(prompt_context=True)
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=120, seq=2)
        h.stt.push(" ".join(WORDS[120:124]))
        scroll = c.wait_type("scroll")
        assert scroll["current_idx"] > 120
    # Las 25 palabras del guion anteriores a la posición, como contexto previo.
    assert h.stt.prompts[0] == " ".join(WORDS[95:120])


def test_desync_y_vuelta_a_listening(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=10, seq=2)
        # El orador dice cosas que no están en el guion: tras varios ciclos el
        # matcher se declara desincronizado y el cliente lo ve.
        h.stt.push(*["lorem ipsum dolor sit amet consectetur"] * 10)
        desync, skipped = c.wait_for(lambda m: m["type"] == "status")
        assert (desync["state"], desync["code"]) == ("desync", None)
        assert not [m for m in skipped if m["type"] == "scroll"]
        assert h.session.matcher.current_idx == 10
        h.wait(lambda: h.stt.pending() == 0, "transcripciones consumidas")
        # Lee un tramo más adelante: resincroniza ahí y vuelve a listening.
        h.stt.push(" ".join(WORDS[60:68]))
        listening, before = c.wait_for(lambda m: m["type"] == "status")
        assert listening["state"] == "listening"
        scrolls = [m for m in before if m["type"] == "scroll"]
        assert scrolls and scrolls[-1]["resync"] is True
        assert 60 < scrolls[-1]["current_idx"] <= 68


def test_set_position_saca_de_desync(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.start(idx=10, seq=2)
        h.stt.push(*["lorem ipsum dolor sit amet consectetur"] * 10)
        c.wait_type("status", state="desync")
        c.send(action="set_position", idx=50, seq=3)
        listening, _ = c.wait_for(lambda m: m["type"] == "status")
        assert listening["state"] == "listening"
        echo = c.wait_type("scroll")
        assert (echo["current_idx"], echo["epoch"]) == (50, 3)


def test_load_script_mientras_escucha_pausa_primero(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        first, _ = c.load(seq=1)
        c.start(idx=30, seq=2)
        long_dash = chr(0x2014)  # guion largo suelto: token sin unidades
        c.send(action="load_script",
               text=f"Hola {long_dash} mundo. [Pausa] Esto es la RTX-5070 ...", seq=5)
        paused = c.next()
        assert (paused["type"], paused["state"]) == ("status", "paused")
        loaded = c.next()
        assert loaded["type"] == "script_loaded" and loaded["epoch"] == 5
        assert loaded["script_id"] != first["script_id"]
        state = c.next()
        assert (state["listening"], state["current_idx"], state["epoch"]) == (False, 0, 5)
        assert state["total_tokens"] == loaded["total_tokens"]
        assert not h.session.listening
        h.wait(lambda: not h.audio.running, "micrófono apagado")


# ---------------------------------------------------------------- envío robusto

def test_guion_con_sustituto_suelto_no_silencia_la_conexion(make) -> None:
    # JSON.stringify de un emoji partido envía "\ud83d" y json.loads lo vuelve
    # un sustituto suelto. Antes, script_loaded no se podía codificar en UTF-8,
    # el emisor moría sin cerrar el socket y cada recarga chocaba igual.
    h = make()
    with h.connect() as c:
        c.hello()
        c.send(action="load_script", text="Hola \ud83d mundo", seq=1)
        loaded = c.wait_type("script_loaded")
        assert loaded["text"] == "Hola � mundo"
        assert [s["text"] for s in loaded["segments"]] == ["Hola", "�", "mundo"]
        assert c.next()["type"] == "state"
        assert c.hello()["script_id"] == loaded["script_id"]  # la conexión sigue respondiendo
        assert c.next()["type"] == "script_loaded"
        for raw in c.raw:
            raw.encode("utf-8")  # el transporte real codifica cada mensaje a UTF-8
    with h.connect() as c2:  # una recarga recupera el guion
        assert c2.hello()["script_id"] == loaded["script_id"]
        assert c2.next()["text"] == "Hola � mundo"
        c2.send(action="set_position", idx=2, seq=2)
        assert c2.wait_type("scroll")["current_idx"] == 2
        for raw in c2.raw:
            raw.encode("utf-8")


def test_fallo_de_envio_cierra_con_1011_y_la_reconexion_recupera(
        make, monkeypatch: pytest.MonkeyPatch) -> None:
    real_send_text = WebSocket.send_text
    failing = threading.Event()
    failing.set()

    async def flaky_send_text(self, data: str) -> None:
        if failing.is_set() and '"type":"script_loaded"' in data:
            raise RuntimeError("fallo simulado del transporte")
        await real_send_text(self, data)

    monkeypatch.setattr(WebSocket, "send_text", flaky_send_text)
    h = make()
    with h.connect() as c1:
        c1.hello()
        c1.send(action="load_script", text=SCRIPT, seq=1)
        code, _reason = c1.wait_close()
        assert code == 1011  # el cliente reconecta en vez de quedar con un socket mudo
        assert h.session.conn is not None and h.session.conn.closed
    h.wait(lambda: h.session.conn is None, "la sesión suelta la conexión rota")
    failing.clear()
    with h.connect() as c2:
        state = c2.hello()
        assert state["script_id"] is not None and state["epoch"] == 1
        assert c2.next()["type"] == "script_loaded"


class _FakeWs:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.sent: list[str] = []
        self.closed: tuple[int, str | None] | None = None

    async def send_text(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)


@pytest.mark.parametrize("error,closed_with", [
    (RuntimeError("transporte roto"), 1011),
    (UnicodeEncodeError("utf-8", "\ud83d", 0, 1, "surrogates not allowed"), 1011),
    (WebSocketDisconnect(1006), None),  # el cliente ya se fue: no hay a quién cerrar
])
def test_emisor_que_falla_cierra_el_socket(error, closed_with) -> None:
    async def run() -> tuple[_FakeWs, Connection]:
        ws = _FakeWs(error)
        conn = Connection(ws)  # type: ignore[arg-type]
        conn.start()
        conn.send(status_msg("paused", "x"))
        await asyncio.wait_for(conn._sender, 1.0)
        conn.send(status_msg("paused", "y"))  # tras el fallo no se encola nada
        return ws, conn

    ws, conn = asyncio.run(run())
    assert conn.closed and len(conn.outbox) == 0
    assert (ws.closed[0] if ws.closed else None) == closed_with


def test_encode_con_sustituto_suelto_es_utf8_valido() -> None:
    msg = {"type": "script_loaded", "text": "Hola \ud83d ñandú \U0001F680"}
    text = encode(msg)
    text.encode("utf-8")
    assert json.loads(text) == msg  # JSON.parse restaura la misma cadena
    assert encode({"t": "ñandú \U0001F680"}) == '{"t":"ñandú \U0001F680"}'  # sin escapes


def test_hello_con_sustituto_en_el_nombre() -> None:
    hello = parse_client_message('{"action": "hello", "client": "web\\ud83d"}', None)
    assert hello == Hello(client="web�")


# ---------------------------------------------------------------- cola de salida

def _types(box: Outbox) -> list[str]:
    return [m["type"] for m in box.pending()]


def test_outbox_fusiona_scroll_y_level_conservando_el_orden() -> None:
    box = Outbox()
    box.put(scroll_msg(current_idx=1, epoch=1))
    box.put(status_msg("listening", "Escuchando"))
    box.put(level_msg(-30.0))
    box.put(scroll_msg(current_idx=2, epoch=1))
    box.put(transcript_msg("hola"))
    box.put(level_msg(-20.0))
    box.put(scroll_msg(current_idx=3, epoch=2))
    assert _types(box) == ["status", "transcript", "level", "scroll"]
    pending = box.pending()
    assert pending[2]["rms_db"] == -20.0
    assert (pending[3]["current_idx"], pending[3]["epoch"]) == (3, 2)


def test_outbox_get_en_orden_y_cierre() -> None:
    async def run() -> list[dict | None]:
        box = Outbox()
        box.put(status_msg("paused", "a"))
        box.put(scroll_msg(current_idx=5, epoch=1))
        got: list[dict | None] = [await box.get(), await box.get()]
        waiter = asyncio.ensure_future(box.get())
        await asyncio.sleep(0)
        assert not waiter.done()
        box.put(level_msg(-40.0))
        got.append(await asyncio.wait_for(waiter, 1.0))
        box.put(status_msg("paused", "b"))
        box.close(4000, "reemplazada")
        got.append(await asyncio.wait_for(box.get(), 1.0))
        box.put(status_msg("paused", "c"))  # tras close no se encola nada
        assert len(box) == 0 and box.close_request == (4000, "reemplazada")
        return got

    got = asyncio.run(run())
    assert [m["type"] if m else None for m in got] == ["status", "scroll", "level", None]


def test_outbox_acotada_descarta_primero_lo_prescindible() -> None:
    box = Outbox()
    box.put(transcript_msg("viejo"))
    for i in range(Outbox.MAX_PENDING):
        box.put(status_msg("paused", str(i)))
    assert len(box) == Outbox.MAX_PENDING
    assert "transcript" not in _types(box)
    box.put(status_msg("paused", "nuevo"))
    pending = box.pending()
    assert len(pending) == Outbox.MAX_PENDING
    assert pending[0]["message"] == "1" and pending[-1]["message"] == "nuevo"


# ---------------------------------------------------------------- protocolo (unidad)

def test_parse_mensajes_validos() -> None:
    assert parse_client_message('{"action": "start"}', 10) == Start(idx=None, seq=None)
    # Sin guion no se acota idx: el servidor responde NO_SCRIPT.
    assert parse_client_message('{"action": "start", "idx": 999}', None) == Start(idx=999)
    assert parse_client_message(
        '{"action": "set_position", "idx": 10, "seq": 7, "extra": 1}', 10) == SetPosition(10, 7)
    assert parse_client_message('{"action": "load_script", "text": " a "}', None) == LoadScript(" a ")


@pytest.mark.parametrize("raw,total", [
    ('{"action": "set_position", "idx": 11}', 10),
    ('{"action": "set_position", "idx": -1}', None),
    ('{"action": "start", "seq": 1.5}', 10),
    ('{"action": "start", "seq": 9007199254740992}', 10),
    ('{"action": "mic_test"}', 10),
    ('{"action": "set_input_device", "device": "3"}', 10),
    (b'{"action": "hello"}', 10),
])
def test_parse_mensajes_invalidos(raw, total) -> None:
    with pytest.raises(ProtocolError) as info:
        parse_client_message(raw, total)
    assert info.value.code == "BAD_REQUEST" and info.value.message


def test_parse_json_demasiado_anidado() -> None:
    with pytest.raises(ProtocolError):
        parse_client_message("[" * 200_000, 10)


def test_constructores_sanean_valores() -> None:
    assert level_msg(float("-inf")) == {"type": "level", "rms_db": -120.0}
    assert level_msg(-32.54)["rms_db"] == -32.5
    assert scroll_msg(current_idx=1, epoch=1, confidence=float("nan"))["confidence"] == 0.0
    assert scroll_msg(current_idx=1, epoch=1, confidence=1.7, stt_ms=-3)["confidence"] == 1.0
    with pytest.raises(ValueError):
        status_msg("error", "x", "NO_EXISTE")
    with pytest.raises(ValueError):
        status_msg("bailando", "x")
    with pytest.raises(ValueError):
        encode({"x": float("nan")})
    assert encode({"t": "ñandú"}) == '{"t":"ñandú"}'
