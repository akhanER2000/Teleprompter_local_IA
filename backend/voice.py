"""Bucle de voz: compone fuente de audio, STT y matcher, y emite `scroll` con época.

Semántica de la Fase 1 (docs/diseno_v2.md, sección 8, último párrafo): cada
`step_ms` toma solo el audio nuevo desde la última lectura y desde el último
reposicionamiento, descarta los tramos silenciosos con el umbral RMS antiguo
y transcribe en un hilo, nunca dos inferencias a la vez. Si la sesión cambió
de generación (reposicionamiento) mientras se transcribía, el resultado se
descarta.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import Any, Protocol

import numpy as np

from backend.config import Settings
from backend.protocol import CODES, level_msg, scroll_msg, transcript_msg
from tools.text_norm import to_units

log = logging.getLogger("teleprompter.voice")

RMS_GATE = 0.005       # umbral heredado; la Fase 2 lo reemplaza por el VAD
MIN_CHUNK_S = 0.5      # Fase 1: bloques nuevos de al menos 500 ms, como antes
MIC_TIMEOUT_S = 1.5    # sin bloques de audio durante este tiempo: MIC_LOST
LEVEL_PERIOD_S = 0.1   # mensajes `level`, unas 10 veces por segundo
STOP_TIMEOUT_S = 5.0   # espera máxima al detener las tareas del bucle
AUDIO_STOP_TIMEOUT_S = 3.0  # espera máxima a que la fuente de audio se detenga
INTERNAL_MESSAGE = "Error interno en el reconocimiento de voz; se pausó la escucha."


class VoiceHost(Protocol):
    """Lo que el bucle usa de la sesión del servidor."""

    epoch: int          # época visible para el cliente (va en cada scroll)
    generation: int     # cambia en cada reposicionamiento; invalida inferencias en curso
    matcher: Any        # ScriptMatcher (docs, sección 5)

    def send(self, msg: dict) -> None: ...

    async def voice_stopped(self, voice: VoiceLoop, code: str, message: str) -> None: ...


class MicLostError(RuntimeError):
    """La fuente de audio dejó de entregar bloques mientras se escuchaba."""


class VoiceLoop:
    """Un ciclo de escucha: se crea en `start` y se descarta al pausar."""

    def __init__(self, host: VoiceHost, audio: Any, stt: Any, settings: Settings, *,
                 infer_lock: threading.Lock | None = None) -> None:
        self._host = host
        self._audio = audio
        self._stt = stt
        self._settings = settings
        # Compartido entre bucles: una inferencia huérfana (bucle ya detenido)
        # impide lanzar otra hasta que termine.
        self._infer_lock = infer_lock if infer_lock is not None else threading.Lock()
        self._since = 0        # muestras anteriores a esta marca se descartan
        self._last_read = 0    # total de muestras en la última lectura
        self._started_at = 0.0
        self._halted = False
        self._task: asyncio.Task | None = None
        self._level_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._halted

    @property
    def audio(self) -> Any:
        """La fuente de audio (la sesión la reutiliza entre bucles)."""
        return self._audio

    async def start(self) -> None:
        """Abre la fuente de audio (lanza si falla) y lanza el bucle."""
        await asyncio.to_thread(self._audio.start)
        self._started_at = time.monotonic()
        # Solo cuenta el audio capturado desde ahora (la fuente puede reutilizarse).
        self._since = self._last_read = self._audio.total_samples
        self._task = asyncio.create_task(self._run(), name="voice-loop")
        self._level_task = asyncio.create_task(self._level_loop(), name="voice-level")

    def discard_audio(self) -> None:
        """Marca como descartado todo el audio capturado hasta ahora."""
        self._since = self._audio.total_samples

    def halt(self) -> None:
        """Detiene el bucle sin esperar: cancela sus tareas y no emite nada más."""
        self._halted = True
        current = asyncio.current_task()
        for task in (self._task, self._level_task):
            if task is not None and task is not current:
                task.cancel()

    async def stop(self) -> bool:
        """Detiene el bucle y la fuente de audio. Idempotente.

        La espera está acotada: si la fuente no se detiene en
        `AUDIO_STOP_TIMEOUT_S` (driver colgado tras desconectar el micrófono)
        se abandona su hilo y devuelve False, para que quien llama la descarte.
        Así una fuente atascada nunca bloquea pausa, reconexión ni apagado.
        """
        self.halt()
        current = asyncio.current_task()
        tasks = [t for t in (self._task, self._level_task) if t is not None and t is not current]
        self._task = self._level_task = None
        if tasks:
            await asyncio.wait(tasks, timeout=STOP_TIMEOUT_S)
        try:
            await asyncio.wait_for(asyncio.to_thread(self._audio.stop), AUDIO_STOP_TIMEOUT_S)
        except TimeoutError:
            log.warning("La fuente de audio no se detuvo en %.1f s; se abandona.",
                        AUDIO_STOP_TIMEOUT_S)
            return False
        except Exception:
            log.warning("Error al detener la fuente de audio.", exc_info=True)
        return True

    # ------------------------------------------------------------ bucle

    def _emit(self, msg: dict) -> None:
        if not self._halted:
            self._host.send(msg)

    async def _run(self) -> None:
        step_s = self._settings.step_ms / 1000.0
        code = message = None
        try:
            next_t = time.monotonic()
            while True:
                next_t += step_s
                delay = next_t - time.monotonic()
                if delay < 0:  # atrasado por la inferencia: el próximo ciclo empieza ya
                    next_t = time.monotonic()
                    delay = 0.0
                await asyncio.sleep(delay)
                self._check_mic()
                await self._cycle()
        except asyncio.CancelledError:
            raise
        except MicLostError as exc:
            log.warning("Micrófono perdido: %s", exc)
            code, message = "MIC_LOST", str(exc)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in CODES:
                # STTError (MODEL_ERROR, MODEL_NOT_READY...): su código y su
                # mensaje en español llegan al usuario, no un INTERNAL genérico.
                log.error("Error en el bucle de voz (%s); se detiene la escucha.", code,
                          exc_info=True)
                message = f"{str(exc).rstrip('. ')}. Se pausó la escucha."
            else:
                log.exception("Error en el bucle de voz; se detiene la escucha.")
                code, message = "INTERNAL", INTERNAL_MESSAGE
        self._task = None  # el bucle terminó solo: stop() no debe esperarse a sí mismo
        if self._halted:
            return
        try:
            await self._host.voice_stopped(self, code, message)
        except Exception:
            log.exception("Error al informar la detención del bucle de voz.")

    def _check_mic(self) -> None:
        last = max(float(self._audio.last_callback_ts or 0.0), self._started_at)
        silent_for = time.monotonic() - last
        if silent_for > MIC_TIMEOUT_S:
            raise MicLostError(
                f"El micrófono dejó de enviar audio ({silent_for:.1f} s sin datos).")

    async def _cycle(self) -> None:
        host = self._host
        generation = host.generation  # se anota antes de tomar el audio
        since = max(self._since, self._last_read)
        rate = getattr(self._audio, "sample_rate", 16000)
        if self._audio.total_samples - since < MIN_CHUNK_S * rate:
            return  # todavía no hay un bloque completo: se acumula para el próximo ciclo
        samples, total = self._audio.read_window(self._settings.window_s, since_sample=since)
        capture_ts = float(self._audio.last_callback_ts or 0.0)
        self._last_read = total
        if samples.size == 0:
            return
        rms = math.sqrt(float(np.mean(np.square(samples, dtype=np.float64))))
        if rms < RMS_GATE:
            return
        result = await asyncio.to_thread(self._infer, samples)
        if self._halted:
            return
        if result is None:  # motor ocupado por una inferencia anterior: se reintenta este audio
            self._last_read = since
            return
        if host.generation != generation:
            log.debug("Resultado descartado: hubo un reposicionamiento durante la inferencia.")
            return
        text = (result.text or "").strip()
        if not text:
            return
        if self._settings.debug:
            self._emit(transcript_msg(text))
        words = to_units(text)
        if not words:
            return
        match = host.matcher.update(words)
        if not match.advanced:
            return
        lag_ms = int((time.monotonic() - capture_ts) * 1000) if capture_ts else 0
        self._emit(scroll_msg(
            current_idx=match.new_idx, epoch=host.epoch, confidence=match.confidence,
            matched_word=match.matched_word, stt_ms=getattr(result, "stt_ms", 0) or 0,
            lag_ms=lag_ms, resync=getattr(match, "resync", False)))

    def _infer(self, samples: np.ndarray) -> Any:
        """Hilo de trabajo: transcribe, o None si otra inferencia sigue en curso."""
        if not self._infer_lock.acquire(blocking=False):
            return None
        try:
            return self._stt.transcribe(samples)
        finally:
            self._infer_lock.release()

    async def _level_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(LEVEL_PERIOD_S)
                self._emit(level_msg(self._audio.level_db))
        except asyncio.CancelledError:
            raise
        except Exception:  # el medidor no debe tumbar la escucha
            log.exception("Error en el medidor de nivel; se desactiva.")
