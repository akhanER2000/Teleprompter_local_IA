"""Servidor FastAPI: sesión persistente, conexión única con reemplazo y cola de salida.

Protocolo WebSocket v2 (docs/diseno_v2.md, sección 6). El puerto abre de
inmediato: el modelo STT y el VAD se cargan en un hilo y su estado llega al
cliente activo como `status` + `state`. `create_app(settings)` es la fábrica
(las pruebas inyectan STT, VAD y audio falsos); `app` es la instancia que
importan uvicorn y launch.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.config import FRONTEND_DIR, SCRIPTS_DIR, Settings, load_settings
from backend.fakes import FakeAudioSource, FakeSTTEngine, FakeVadGate
from backend.protocol import (
    ControlCmd, Estado, Hello, LoadScript, Pause, ProtocolError, SetPosition, Start,
    control_error_msg, control_forward_msg, echo_scroll_msg, encode, estado_control_msg,
    parse_client_message, parse_control_message, script_loaded_msg, state_msg, status_msg,
)
from backend.voice import LISTENING_MESSAGE, SAMPLE_RATE, VAD_TAIL_S, VoiceLoop
from tools.voice_brakes import BrakeParams
from tools.script_matcher import ScriptMatcher
from tools.script_parser import ParsedScript, parse_script

log = logging.getLogger("teleprompter")

CLOSE_REPLACED = 4000      # el cliente no reintenta: "Abierto en otra ventana"
CLOSE_GOING_AWAY = 1001
CLOSE_INTERNAL_ERROR = 1011  # falló un envío: el cliente reconecta y se resincroniza
BG_TIMEOUT_S = 5.0
CLOSE_POLICY = 1008          # /control: origen no permitido
ESTADO_PERIOD_S = 1.0        # /control: el estado se difunde al cambiar y cada segundo


SttFactory = Callable[[Settings], Any]
AudioFactory = Callable[[Settings], Any]
VadFactory = Callable[[Settings], Any]


def default_stt_factory(settings: Settings) -> Any:
    """Motor STT según la configuración. Se llama en el hilo de carga."""
    if settings.fake_stt:
        return FakeSTTEngine(model_size=settings.model_size, device=settings.device,
                             compute_type=settings.compute_type, language=settings.language)
    from tools.stt_engine import STTEngine  # importa faster-whisper: solo aquí

    return STTEngine(model_size=settings.model_size, device=settings.device,
                     compute_type=settings.compute_type, language=settings.language)


def default_vad_factory(settings: Settings) -> Any:
    """Detector de voz (Silero) según la configuración. Se llama en el hilo de carga."""
    if settings.fake_stt:
        return FakeVadGate(threshold=settings.vad_threshold)
    from tools.vad_gate import VadGate  # importa faster-whisper y onnxruntime: solo aquí

    return VadGate(threshold=settings.vad_threshold)


def warm_up_vad(vad: Any) -> None:
    """Fuerza la carga perezosa de Silero con 1 s de silencio (el primer ciclo no espera)."""
    vad.has_speech(np.zeros(int(VAD_TAIL_S * SAMPLE_RATE), dtype=np.float32), tail_s=VAD_TAIL_S)


def build_script(text: str) -> tuple[ParsedScript, ScriptMatcher]:
    """Parsea el guion y prepara su matcher (en un hilo: un guion enorme tarda)."""
    parsed = parse_script(text)
    return parsed, ScriptMatcher(parsed.tokens)


def default_audio_factory(settings: Settings) -> Any:
    """Fuente de audio: falsa (TP_FAKE_STT), un WAV (TP_AUDIO_FILE) o el micrófono."""
    if settings.fake_stt:
        return FakeAudioSource()
    if settings.audio_file:
        from tools.audio_file_source import AudioFileSource

        return AudioFileSource(settings.audio_file, window_s=settings.window_s)
    from tools.audio_capture import AudioCapture  # importa sounddevice: solo aquí

    return AudioCapture(device=settings.input_device, window_s=settings.window_s)


# ---------------------------------------------------------------- salida

class Outbox:
    """Cola de salida de una conexión.

    Si hay varios `scroll` (o varios `level`) pendientes, solo queda el
    último de cada tipo, al final; el resto conserva su orden. `close()`
    descarta lo pendiente y pide al emisor que cierre el socket.
    """

    COALESCED = frozenset({"scroll", "level", "activity", "estado"})
    MAX_PENDING = 256  # cliente que no lee: se pierde primero lo prescindible

    def __init__(self) -> None:
        self._items: deque[dict] = deque()
        self._wakeup = asyncio.Event()
        self.close_request: tuple[int, str] | None = None

    def __len__(self) -> int:
        return len(self._items)

    def pending(self) -> list[dict]:
        return list(self._items)

    def put(self, msg: dict) -> None:
        if self.close_request is not None:
            return
        kind = msg.get("type")
        if kind in self.COALESCED:
            self._remove_first(kind)  # como mucho hay uno pendiente de cada tipo
        self._items.append(msg)
        if len(self._items) > self.MAX_PENDING:
            if not (self._remove_first("transcript") or self._remove_first("level")):
                self._items.popleft()
        self._wakeup.set()

    def _remove_first(self, kind: str) -> bool:
        for i, item in enumerate(self._items):
            if item.get("type") == kind:
                del self._items[i]
                return True
        return False

    def close(self, code: int, reason: str) -> None:
        if self.close_request is None:
            self.close_request = (code, reason)
            self._items.clear()
            self._wakeup.set()

    async def get(self) -> dict | None:
        """Siguiente mensaje, o None si se pidió cerrar."""
        while True:
            if self.close_request is not None:
                return None
            if self._items:
                return self._items.popleft()
            self._wakeup.clear()
            await self._wakeup.wait()


class Connection:
    """Una conexión WebSocket con su cola y su única tarea emisora."""

    _ids = itertools.count(1)

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.id = next(self._ids)
        self.outbox = Outbox()
        self.client_name: str | None = None
        self._closed = False
        self._sender: asyncio.Task | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        self._sender = asyncio.create_task(self._send_loop(), name=f"ws-sender-{self.id}")

    def send(self, msg: dict) -> None:
        if not self._closed:
            self.outbox.put(msg)

    def close(self, code: int, reason: str) -> None:
        """Cierra tras descartar lo pendiente (lo hace la tarea emisora)."""
        if not self._closed:
            self._closed = True
            self.outbox.close(code, reason)

    def abort(self) -> None:
        """El socket ya se cerró: detiene la tarea emisora sin enviar nada."""
        self._closed = True
        if self._sender is not None and not self._sender.done():
            self._sender.cancel()

    async def _send_loop(self) -> None:
        try:
            while True:
                msg = await self.outbox.get()
                if msg is None:
                    code, reason = self.outbox.close_request or (CLOSE_GOING_AWAY, "")
                    await self.ws.close(code=code, reason=reason)
                    return
                try:
                    text = encode(msg)
                except (TypeError, ValueError):
                    log.exception("Mensaje %r no serializable; se descarta.", msg.get("type"))
                    continue
                await self.ws.send_text(text)
        except asyncio.CancelledError:
            raise
        except WebSocketDisconnect:  # el cliente ya cerró el socket
            self._closed = True
            log.debug("Conexión %d: el cliente cerró durante un envío.", self.id)
        except Exception as exc:
            # Un emisor muerto con el socket abierto dejaría al cliente sin
            # respuestas y sin motivo para reconectar (y a la sesión con una
            # conexión zombi): se cierra el socket. Al recibir el cierre, el
            # bucle de recepción termina y `detach` limpia la sesión.
            self._closed = True
            request = self.outbox.close_request
            if request is None:
                log.warning("Conexión %d: falló un envío (%s: %s); se cierra la conexión.",
                            self.id, type(exc).__name__, exc)
                request = (CLOSE_INTERNAL_ERROR, "error al enviar")
            else:
                log.debug("Conexión %d: falló el cierre (%s).", self.id, exc)
            with contextlib.suppress(Exception):
                await self.ws.close(code=request[0], reason=request[1])


# ---------------------------------------------------------------- sesión

class Session:
    """Estado que sobrevive a las reconexiones: guion, posición, época, modelo y escucha."""

    def __init__(self, settings: Settings, stt_factory: SttFactory,
                 audio_factory: AudioFactory, vad_factory: VadFactory) -> None:
        self.settings = settings
        self._stt_factory = stt_factory
        self._audio_factory = audio_factory
        self._vad_factory = vad_factory
        self.stt: Any = None
        self.vad: Any = None   # VadGate: uno por sesión, cargado junto con el modelo
        self.model_state = "loading"
        self.model_code: str | None = None
        self.model_message = f"Cargando el modelo {settings.model_size}…"
        self.device_in_use = settings.device
        self.cuda_fallback = False
        self.script: ParsedScript | None = None
        self.script_name: str | None = None   # nombre en guiones/ si vino de open_script
        self.matcher = ScriptMatcher()
        # Hito 84 (T6): controles conectados a /control y último estado del visor.
        self.controls: set[Connection] = set()
        self.estado: dict | None = None
        self.epoch = 0         # último `seq` adoptado; va en cada scroll
        self.generation = 0    # sube en cada reposicionamiento (aunque falte `seq`)
        self.voice: VoiceLoop | None = None
        self.conn: Connection | None = None
        self._infer_lock = threading.Lock()
        # La fuente de audio se crea en el primer start y se reutiliza: un WAV
        # (TP_AUDIO_FILE) sigue donde quedó al reanudar en vez de volver a 0.
        self._audio: Any = None
        self._start_lock = asyncio.Lock()      # un start a la vez abre la fuente
        self._stopping: set[asyncio.Task] = set()  # detenciones de bucles en curso
        self._loop: asyncio.AbstractEventLoop | None = None
        self._voice_brakes: BrakeParams | None = None
        self._closed = False
        self._bg: set[asyncio.Task] = set()

    @property
    def listening(self) -> bool:
        return self.voice is not None

    def state(self) -> dict:
        script = self.script
        return state_msg(
            model=self.model_state, model_name=self.settings.model_size,
            device=self.device_in_use, listening=self.listening,
            script_id=script.script_id if script else None,
            total_tokens=script.total_tokens if script else 0,
            current_idx=self.shown_idx() if script else 0,
            epoch=self.epoch, debug=self.settings.debug)

    def shown_idx(self) -> int:
        """Posición que ve el lector: la de los frenos en Voz, si no la del matcher."""
        voice = self.voice
        if voice is not None and voice.brakes is not None:
            return voice.brakes.displayed
        return self.matcher.current_idx

    def script_loaded(self) -> dict:
        assert self.script is not None
        return script_loaded_msg(
            script_id=self.script.script_id, total_tokens=self.script.total_tokens,
            epoch=self.epoch, text=self.script.text, segments=self.script.segments,
            name=self.script_name)

    def send(self, msg: dict) -> None:
        """Envía al cliente activo, si hay uno."""
        conn = self.conn
        if conn is not None:
            conn.send(msg)

    def _active(self, conn: Connection) -> bool:
        # `conn.closed`: su emisor murió (el socket se está cerrando); no debe
        # abrir el micrófono ni cambiar la sesión sin poder responder.
        return conn is self.conn and not conn.closed and not self._closed

    # ------------------------------------------------------------ modelo

    def start_model_loading(self) -> None:
        """Carga el modelo en un hilo daemon: el puerto abre sin esperar."""
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._load_model, name="stt-load", daemon=True).start()

    def _load_model(self) -> None:  # hilo de carga
        s = self.settings
        log.info("Cargando el modelo STT%s '%s' en %s (%s)...", " falso" if s.fake_stt else "",
                 s.model_size, s.device, s.compute_type)
        t0 = time.monotonic()
        try:
            engine = self._stt_factory(s)
            engine.load()
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in ("MODEL_MISSING", "MODEL_ERROR"):  # STTError: mensaje ya en español
                log.error("No se pudo cargar el modelo (%s): %s", code, exc,
                          exc_info=code == "MODEL_ERROR")
                message = str(exc)
            else:
                log.exception("No se pudo cargar el modelo.")
                code, message = "MODEL_ERROR", f"No se pudo cargar el modelo {s.model_size}: {exc}"
            self._call_in_loop(self._on_model_failed, code, message)
            return
        log.info("Modelo cargado en %.1f s.", time.monotonic() - t0)
        t1 = time.monotonic()
        try:
            vad = self._vad_factory(s)
            warm_up_vad(vad)
        except Exception as exc:
            log.exception("No se pudo cargar el detector de voz (Silero VAD).")
            self._call_in_loop(self._on_model_failed, "MODEL_ERROR",
                               f"No se pudo cargar el detector de voz (Silero VAD): {exc}")
            return
        log.info("Detector de voz listo en %.0f ms.", (time.monotonic() - t1) * 1000)
        self._call_in_loop(self._on_model_ready, engine, vad)

    def _call_in_loop(self, fn: Callable[..., None], *args: Any) -> None:
        try:
            assert self._loop is not None
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # el loop ya se cerró (apagado durante la carga)
            log.debug("Loop cerrado: se ignora el resultado de la carga del modelo.")

    def _on_model_ready(self, engine: Any, vad: Any) -> None:
        if self._closed:
            return
        self.stt = engine
        self.vad = vad
        self.model_state, self.model_code = "ready", None
        self.device_in_use = getattr(engine, "device_in_use", None) or self.settings.device
        self.cuda_fallback = self.settings.device == "cuda" and self.device_in_use == "cpu"
        self.model_message = f"Modelo {self.settings.model_size} listo ({self.device_in_use})."
        if self.cuda_fallback:
            log.warning("CUDA no disponible: el modelo corre en CPU.")
        self.send(status_msg("ready", self.model_message))
        for msg in self._model_status_msgs():
            self.send(msg)
        self.send(self.state())

    def _on_model_failed(self, code: str, message: str) -> None:
        if self._closed:
            return
        self.model_state, self.model_code, self.model_message = "error", code, message
        for msg in self._model_status_msgs():
            self.send(msg)
        self.send(self.state())

    def _model_status_msgs(self) -> list[dict]:
        """Avisos del modelo que un cliente debe ver (al cambiar o al saludar)."""
        if self.model_state == "loading":
            return [status_msg("loading", self.model_message)]
        if self.model_state == "error":
            return [status_msg("error", self.model_message, self.model_code)]
        if self.cuda_fallback:
            reason = getattr(self.stt, "fallback_reason", None)
            detail = f" ({reason})" if reason else ""
            return [status_msg("warning", "CUDA no está disponible: el modelo corre en CPU, "
                                          f"más lento{detail}.", "CUDA_FALLBACK")]
        return []

    # ------------------------------------------------------------ conexiones

    async def attach(self, conn: Connection) -> None:
        """La conexión nueva pasa a ser la activa y reemplaza a la anterior."""
        conn.start()
        if self._closed:
            conn.close(CLOSE_GOING_AWAY, "servidor detenido")
            return
        old, self.conn = self.conn, conn
        if old is not None:
            log.info("Conexión %d reemplaza a la %d.", conn.id, old.id)
            old.close(CLOSE_REPLACED, "reemplazada")
            # Sin esperar a la fuente de audio: el cliente nuevo empieza a
            # recibir ya, y un start posterior espera a que termine de pararse.
            if self._halt_listening() is not None:
                log.info("Escucha detenida: la conexión anterior fue reemplazada.")

    async def detach(self, conn: Connection) -> None:
        conn.abort()
        if self.conn is not conn:
            return
        self.conn = None
        if self._halt_listening() is not None:
            log.info("Escucha detenida: el cliente se desconectó.")
        self.broadcast_estado()   # los controles ven "conectado: false"

    # ------------------------------------------------------------ /control (T6)

    def viewer_connected(self) -> bool:
        conn = self.conn
        return conn is not None and not conn.closed and not self._closed

    def estado_msg(self) -> dict:
        return estado_control_msg(self.estado, conectado=self.viewer_connected())

    def broadcast_estado(self) -> None:
        if not self.controls:
            return
        msg = self.estado_msg()
        for ctl in list(self.controls):
            ctl.send(msg)

    async def attach_control(self, conn: Connection) -> None:
        conn.start()
        if self._closed:
            conn.close(CLOSE_GOING_AWAY, "servidor detenido")
            return
        self.controls.add(conn)
        log.info("Control %d conectado (%d en total).", conn.id, len(self.controls))
        conn.send(self.estado_msg())

    def detach_control(self, conn: Connection) -> None:
        conn.abort()
        self.controls.discard(conn)
        log.info("Control %d desconectado (%d en total).", conn.id, len(self.controls))

    async def handle_control_raw(self, conn: Connection, raw: str | bytes) -> None:
        """Valida una orden de un control y la reenvía al visor. Nunca cierra la conexión."""
        try:
            cmd = parse_control_message(raw)
        except ProtocolError as exc:
            log.info("Orden de control inválida: %s", exc.message)
            conn.send(control_error_msg(exc.message, exc.code))
            return
        try:
            if cmd.cmd == "open_script":
                await self._open_script(conn, cmd)
                return
            if not self.viewer_connected():
                conn.send(self.estado_msg())   # conectado: false; la orden se descarta
                return
            self.send(control_forward_msg(cmd))
        except Exception:
            log.exception("Error al procesar la orden de control %r.", cmd.cmd)
            conn.send(control_error_msg("Error interno al procesar la orden.", "INTERNAL"))

    async def _open_script(self, ctl: Connection, cmd: ControlCmd) -> None:
        """Carga `guiones/<nombre>` (con o sin .txt) sin salir de la carpeta y lo manda al visor."""
        path = resolve_script(cmd.value)
        if path is None:
            ctl.send(control_error_msg(f"No existe el guion '{cmd.value}' en guiones/."))
            return
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            ctl.send(control_error_msg(f"No se pudo leer el guion: {exc}"))
            return
        if not self.viewer_connected():
            ctl.send(self.estado_msg())
            return
        if await self._stop_listening():
            self.send(status_msg("paused", "Escucha pausada para cargar el guion."))
        try:
            parsed, matcher = await asyncio.to_thread(build_script, text)
        except ValueError as exc:
            ctl.send(control_error_msg(f"No se pudo cargar el guion: {exc}"))
            return
        self.script = parsed
        self.script_name = path.name
        self.matcher = matcher
        self._reposition(None, 0)
        log.info("Guion '%s' abierto desde un control: %d palabras.", path.name, parsed.total_tokens)
        self.send(self.state())
        self.send(self.script_loaded())

    async def handle_raw(self, conn: Connection, raw: str | bytes) -> None:
        """Valida y despacha un mensaje del cliente. Nunca cierra la conexión."""
        if not self._active(conn):
            return  # conexión reemplazada: se ignora
        total = self.script.total_tokens if self.script is not None else None
        try:
            msg = parse_client_message(raw, total)
        except ProtocolError as exc:
            log.info("Mensaje inválido: %s", exc.message)
            conn.send(status_msg("error", exc.message, exc.code))
            return
        try:
            await self._dispatch(conn, msg)
        except Exception:
            log.exception("Error al procesar la acción %r.", msg.action)
            if self._active(conn):
                conn.send(status_msg("error", "Error interno al procesar la acción.", "INTERNAL"))

    async def _dispatch(self, conn: Connection, msg: Any) -> None:
        if isinstance(msg, Hello):
            self._on_hello(conn, msg)
        elif isinstance(msg, LoadScript):
            await self._on_load_script(conn, msg)
        elif isinstance(msg, Start):
            await self._on_start(conn, msg)
        elif isinstance(msg, Pause):
            await self._on_pause(conn)
        elif isinstance(msg, SetPosition):
            self._on_set_position(conn, msg)
        elif isinstance(msg, Estado):
            self.estado = msg.fields
            self.broadcast_estado()
        else:  # set_input_device y mic_test: Fase 6
            conn.send(status_msg("error", f"La acción '{msg.action}' no está disponible todavía.",
                                 "BAD_REQUEST"))

    # ------------------------------------------------------------ acciones

    def _on_hello(self, conn: Connection, msg: Hello) -> None:
        conn.client_name = msg.client
        log.info("Conexión %d: hello de %s.", conn.id, msg.client or "un cliente sin nombre")
        conn.send(self.state())
        if self.script is not None:
            conn.send(self.script_loaded())
        for status in self._model_status_msgs():
            conn.send(status)

    async def _on_load_script(self, conn: Connection, msg: LoadScript) -> None:
        if await self._stop_listening():
            if not self._active(conn):
                return
            conn.send(status_msg("paused", "Escucha pausada para cargar el guion."))
        try:
            parsed, matcher = await asyncio.to_thread(build_script, msg.text)
        except ValueError as exc:
            if self._active(conn):
                conn.send(status_msg("error", f"No se pudo cargar el guion: {exc}", "BAD_REQUEST"))
            return
        if not self._active(conn):
            return
        self.script = parsed
        self.script_name = None
        self.matcher = matcher
        self._reposition(msg.seq)
        log.info("Guion cargado: %d palabras.", parsed.total_tokens)
        conn.send(self.script_loaded())
        conn.send(self.state())

    async def _on_start(self, conn: Connection, msg: Start) -> None:
        """Atómico: posición y escucha, o error sin cambiar nada."""
        if self.script is None:
            self._reject_start(conn, "NO_SCRIPT", "Carga un guion antes de iniciar Manos libres.")
            return
        if self.model_state != "ready" or self.stt is None or self.vad is None:
            if self.model_state == "loading":
                text = "El modelo de voz todavía se está cargando."
            else:
                text = f"El modelo de voz no está disponible: {self.model_message}"
            self._reject_start(conn, "MODEL_NOT_READY", text)
            return
        if self.voice is not None and (self.voice.mode != (msg.mode or "voz")
                                       or self._brake_params(msg) != self._voice_brakes):
            await self._stop_listening()   # cambio de modo o de frenos: bucle nuevo
            if not self._active(conn):
                return
        if self.voice is None:
            async with self._start_lock:
                # Una detención anterior (pausa, MIC_LOST, reemplazo) tiene que
                # terminar antes de reabrir la misma fuente; si no, su stop()
                # tardío apagaría la fuente del bucle nuevo.
                await self._wait_stopping()
                if not self._active(conn):
                    return
                if self.voice is None:  # otro start pudo instalarlo mientras se esperaba
                    if not await self._open_voice(conn, msg):
                        return
        idx = self.matcher.current_idx if msg.idx is None else msg.idx
        self._reposition(msg.seq, idx)
        conn.send(status_msg("listening", LISTENING_MESSAGE))
        conn.send(echo_scroll_msg(self.matcher.current_idx, self.epoch))

    @staticmethod
    def _brake_params(msg: Start) -> BrakeParams | None:
        if msg.mode != "voz":
            return None   # Asistido no usa frenos; sin modo (cliente v2), Voz como en la Fase 2
        return BrakeParams(**(msg.brakes or {}))

    async def _open_voice(self, conn: Connection, msg: Start) -> bool:
        """Abre la fuente de audio y lanza el bucle (con `_start_lock` tomado).

        Devuelve True si quedó escuchando. Si falla responde MIC_ERROR; si la
        conexión dejó de ser la activa mientras se abría, lo descarta.
        """
        voice: VoiceLoop | None = None
        try:
            if self._audio is None:
                self._audio = await asyncio.to_thread(self._audio_factory, self.settings)
            brakes = self._brake_params(msg)
            voice = VoiceLoop(self, self._audio, self.stt, self.settings, vad=self.vad,
                              infer_lock=self._infer_lock, mode=msg.mode or "voz",
                              brakes=brakes)
            self._voice_brakes = brakes
            await voice.start()
        except asyncio.CancelledError:
            if voice is not None:
                self._stop_voice(voice)
            raise
        except Exception as exc:
            log.warning("No se pudo abrir el micrófono: %s", exc)
            if voice is not None:
                await asyncio.shield(self._stop_voice(voice))
            if self._active(conn):
                self._reject_start(conn, "MIC_ERROR", f"No se pudo abrir el micrófono: {exc}")
            return False
        if not self._active(conn):  # otra conexión tomó el control mientras se abría
            await asyncio.shield(self._stop_voice(voice))
            return False
        self.voice = voice
        log.info("Escuchando.")
        return True

    def _reject_start(self, conn: Connection, code: str, message: str) -> None:
        conn.send(status_msg("error", message, code))
        conn.send(self.state())

    async def _on_pause(self, conn: Connection) -> None:
        if await self._stop_listening():
            log.info("Escucha pausada.")
        if self._active(conn):
            conn.send(status_msg("paused", "Pausado."))

    def _on_set_position(self, conn: Connection, msg: SetPosition) -> None:
        if self.script is None:
            conn.send(status_msg("error", "No hay guion cargado.", "NO_SCRIPT"))
            return
        self._reposition(msg.seq, msg.idx)
        conn.send(echo_scroll_msg(self.matcher.current_idx, self.epoch))

    def _reposition(self, seq: int | None, idx: int | None = None) -> None:
        """Aplica posición y época e invalida el audio y la inferencia en curso."""
        if idx is not None:
            self.matcher.set_position(idx)
        if seq is not None:
            self.epoch = seq
        self.generation += 1
        if self.voice is not None:
            self.voice.reposition()

    # ------------------------------------------------------------ escucha

    async def _stop_listening(self) -> bool:
        """Detiene la escucha y espera a la fuente de audio. Devuelve si escuchaba."""
        task = self._halt_listening()
        if task is None:
            return False
        await asyncio.shield(task)  # acotada por VoiceLoop.stop
        return True

    def _halt_listening(self) -> asyncio.Task | None:
        """Deja de escuchar ya (síncrono) y lanza la detención de la fuente.

        Devuelve la tarea de detención, o None si no escuchaba. Desde aquí el
        bucle no emite nada más y `listening` es False.
        """
        voice, self.voice = self.voice, None
        if voice is None:
            return None
        if voice.brakes is not None:
            # El matcher puede ir por delante de lo mostrado: manda lo que vio el lector.
            self.matcher.set_position(voice.brakes.displayed)
        return self._stop_voice(voice)

    def _stop_voice(self, voice: VoiceLoop) -> asyncio.Task:
        """Detiene un bucle en segundo plano; `_wait_stopping` la espera antes de un start."""
        voice.halt()  # síncrono: el bucle ya no emite nada
        task = self._spawn(self._finish_stop(voice))
        self._stopping.add(task)
        task.add_done_callback(self._stopping.discard)
        return task

    async def _finish_stop(self, voice: VoiceLoop) -> None:
        if not await voice.stop() and voice.audio is self._audio:
            log.warning("La fuente de audio quedó atascada; el próximo start crea otra.")
            self._audio = None

    async def _wait_stopping(self) -> None:
        pending = [t for t in self._stopping if not t.done()]
        if pending:
            await asyncio.wait(pending)  # cada una está acotada por VoiceLoop.stop

    async def voice_stopped(self, voice: VoiceLoop, code: str, message: str) -> None:
        """El bucle de voz terminó por un error (MIC_LOST, MODEL_ERROR, INTERNAL...).

        Primero se informa y después se detiene la fuente: un driver lento al
        cerrar un micrófono que desapareció no retrasa el aviso, y ningún
        mensaje de un start posterior puede quedar antes que este error.
        """
        if voice is not self.voice:
            return
        self._halt_listening()
        self.send(status_msg("error", message, code))
        self.send(self.state())

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def shutdown(self) -> None:
        self._closed = True
        self._halt_listening()
        conn, self.conn = self.conn, None
        if conn is not None:
            conn.close(CLOSE_GOING_AWAY, "servidor detenido")
        for ctl in list(self.controls):
            ctl.close(CLOSE_GOING_AWAY, "servidor detenido")
        self.controls.clear()
        pending = [t for t in self._bg if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=BG_TIMEOUT_S)


def resolve_script(name: str, base=None):
    """`guiones/<name>` o `guiones/<name>.txt`, solo si queda dentro de la carpeta; si no, None."""
    base = (base or SCRIPTS_DIR).resolve()
    for candidate in (name, f"{name}.txt"):
        path = (base / candidate).resolve()
        if path.parent == base and path.is_file():
            return path
    return None


def server_mod_scripts_dir():
    """La carpeta de guiones (las pruebas la cambian con monkeypatch de SCRIPTS_DIR)."""
    return SCRIPTS_DIR


def control_origin_ok(origin: str | None, port: int) -> bool:
    """/control acepta clientes sin Origin (el plugin de Node) o la propia app en 127.0.0.1."""
    if origin is None:
        return True
    return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


# ---------------------------------------------------------------- app

def create_app(settings: Settings | None = None, *,
               stt_factory: SttFactory | None = None,
               audio_factory: AudioFactory | None = None,
               vad_factory: VadFactory | None = None) -> FastAPI:
    """Crea la app. Las fábricas permiten inyectar STT, audio y VAD (pruebas)."""
    # No hace nada si el lanzador ya configuró el logging raíz (launch.py).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if settings is None:
        settings = load_settings()
    logging.getLogger("teleprompter").setLevel(settings.log_level)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)  # una línea INFO por inferencia
    session = Session(settings, stt_factory or default_stt_factory,
                      audio_factory or default_audio_factory,
                      vad_factory or default_vad_factory)

    async def estado_ticker() -> None:
        while True:
            await asyncio.sleep(ESTADO_PERIOD_S)
            session.broadcast_estado()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("Configuración: %s", settings)
        session.start_model_loading()
        ticker = asyncio.create_task(estado_ticker(), name="estado-ticker")
        try:
            yield
        finally:
            ticker.cancel()
            await session.shutdown()

    app = FastAPI(title="Teleprompter Local IA", lifespan=lifespan)
    app.state.settings = settings
    app.state.session = session
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)   # sin icono: evita un 404 en cada carga

    @app.get("/api/guiones")
    async def list_scripts() -> list[str]:
        """Hito 84: nombres de los .txt de guiones/ (para el selector y los controles)."""
        base = server_mod_scripts_dir()
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".txt")

    @app.get("/api/guiones/{name}")
    async def read_script(name: str):
        path = resolve_script(name, server_mod_scripts_dir())
        if path is None:
            raise HTTPException(status_code=404, detail="No existe ese guion en guiones/.")
        text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
        return PlainTextResponse(text)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        # Fase 4: validar aquí ws.headers.get("origin") y cerrar con 1008 antes de accept().
        await ws.accept()
        conn = Connection(ws)
        try:
            await session.attach(conn)
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    raw = message.get("bytes") or b""  # binario: BAD_REQUEST
                await session.handle_raw(conn, raw)
        finally:
            await session.detach(conn)

    @app.websocket("/control")
    async def control_endpoint(ws: WebSocket) -> None:
        # Hito 84 (T6): varios controles a la vez; no tocan al visor de /ws.
        if not control_origin_ok(ws.headers.get("origin"), settings.port):
            log.warning("/control rechazado: origen %r.", ws.headers.get("origin"))
            await ws.close(code=CLOSE_POLICY)
            return
        await ws.accept()
        conn = Connection(ws)
        try:
            await session.attach_control(conn)
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    raw = message.get("bytes") or b""
                await session.handle_control_raw(conn, raw)
        finally:
            session.detach_control(conn)

    return app


app = create_app()
