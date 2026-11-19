"""Bucle de voz: compone fuente de audio, VAD, STT, filtro y matcher, y emite `scroll` con época.

Semántica de la Fase 2 (docs/diseno_v2.md, sección 8):

1. Cada `step_ms` toma la ventana de los últimos `window_s` segundos
   posteriores al último reposicionamiento (ventanas solapadas: cada ciclo ve
   también parte del audio del anterior, así las palabras no quedan partidas).
2. Si la ventana dura menos de `MIN_WINDOW_S`, o el VAD (Silero) no ve voz en
   su último `VAD_TAIL_S`, no transcribe. Ya no hay umbral RMS.
3. Transcribe con el `initial_prompt` del guion previo (solo con
   `prompt_context`), filtra alucinaciones, normaliza con `to_units` y llama
   al matcher.
4. La inferencia se espera dentro del bucle: nunca hay dos a la vez ni se
   acumula retraso, porque el ciclo siguiente toma siempre la ventana más
   reciente. Un candado compartido entre bucles impide además que un bucle
   nuevo transcriba mientras sigue en curso la inferencia huérfana de uno
   detenido (se salta el ciclo).
5. Si la sesión cambió de generación (reposicionamiento) mientras se
   transcribía, el resultado se descarta.

`VoiceCycle` es la parte síncrona y sin estado del ciclo (VAD, STT, filtro,
normalización y matcher). La usan este bucle y `tools/replay.py`, así el
replay mide exactamente el pipeline en vivo.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from backend.config import Settings
from backend.protocol import CODES, level_msg, scroll_msg, status_msg, transcript_msg
from tools.text_norm import to_units
from tools.transcript_filter import clean_transcription

log = logging.getLogger("teleprompter.voice")

SAMPLE_RATE = 16000
MIN_WINDOW_S = 1.0     # ventanas más cortas no se transcriben (Whisper casi sin contexto)
VAD_TAIL_S = 1.0       # el VAD mira solo el último tramo de la ventana
PROMPT_WORDS = 25      # palabras del guion previas a current_idx para el initial_prompt
MIC_TIMEOUT_S = 1.5    # sin bloques de audio durante este tiempo: MIC_LOST
LEVEL_PERIOD_S = 0.1   # mensajes `level`, unas 10 veces por segundo
STOP_TIMEOUT_S = 5.0   # espera máxima al detener las tareas del bucle
AUDIO_STOP_TIMEOUT_S = 3.0  # espera máxima a que la fuente de audio se detenga
INTERNAL_MESSAGE = "Error interno en el reconocimiento de voz; se pausó la escucha."
LISTENING_MESSAGE = "Escuchando…"
DESYNC_MESSAGE = "Buscando la posición en el guion…"
DROPPED_PREFIX = "[descartado: {reason}] "  # transcript descartado (solo con TP_DEBUG)


# ---------------------------------------------------------------- ciclo síncrono

@dataclass
class CycleOutput:
    """Resultado de `VoiceCycle.transcribe`.

    `voiced` es False si no se llamó al STT (ventana corta o sin voz). Con
    `dropped` (motivo del filtro), `text` es la transcripción descartada y
    `words` queda vacía; si no, `text` es lo que sobrevivió al filtro y
    `words` sus unidades.
    """

    voiced: bool
    text: str = ""
    words: list[str] = field(default_factory=list)
    stt_ms: int = 0
    dropped: str | None = None


class VoiceCycle:
    """Un ciclo del pipeline de voz, síncrono y sin estado propio.

    `transcribe` corre en un hilo de trabajo (VAD y STT bloquean); `apply`
    corre en el hilo dueño del matcher (el event loop en vivo), después de
    comprobar la generación.
    """

    def __init__(self, stt: Any, vad: Any, settings: Settings) -> None:
        self._stt = stt
        self._vad = vad
        self._settings = settings

    def transcribe(self, window: np.ndarray, *, prompt: str | None,
                   near_text: str) -> CycleOutput:
        """VAD sobre la cola de la ventana, STT, filtro de alucinaciones y `to_units`.

        `prompt` (texto del guion previo) solo llega al STT con
        `prompt_context`; `near_text` (guion alrededor de la posición) evita
        descartar como alucinación una frase que el guion sí contiene.
        """
        samples = np.asarray(window, dtype=np.float32).reshape(-1)
        if samples.size < int(MIN_WINDOW_S * SAMPLE_RATE):
            return CycleOutput(voiced=False)
        if not self._vad.has_speech(samples, tail_s=VAD_TAIL_S):
            return CycleOutput(voiced=False)
        initial_prompt = (prompt or None) if self._settings.prompt_context else None
        result = self._stt.transcribe(samples, initial_prompt=initial_prompt)
        stt_ms = max(0, int(getattr(result, "stt_ms", 0) or 0))
        outcome = clean_transcription(result, near_text=near_text or "")
        if outcome.reason is not None:
            # Texto completo de Whisper: solo para el log y el transcript de depuración.
            raw = getattr(outcome, "original", "") or (getattr(result, "text", "") or "")
            return CycleOutput(voiced=True, text=raw.strip(), stt_ms=stt_ms,
                               dropped=outcome.reason)
        text = (outcome.text or "").strip()
        return CycleOutput(voiced=True, text=text, words=to_units(text), stt_ms=stt_ms)

    @staticmethod
    def apply(out: CycleOutput, matcher: Any) -> Any:
        """Llama al matcher con las palabras del ciclo; None si no hubo voz o palabras.

        Las transcripciones vacías o filtradas no llegan al matcher, así que
        no cuentan como ciclo para su detección de desincronización.
        """
        if not out.voiced or not out.words:
            return None
        return matcher.update(out.words)


# ---------------------------------------------------------------- bucle asíncrono

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
                 vad: Any, infer_lock: threading.Lock | None = None) -> None:
        self._host = host
        self._audio = audio
        self._settings = settings
        self._cycle_runner = VoiceCycle(stt, vad, settings)
        # Compartido entre bucles: una inferencia huérfana (bucle ya detenido)
        # impide lanzar otra hasta que termine.
        self._infer_lock = infer_lock if infer_lock is not None else threading.Lock()
        self._since = 0        # muestras anteriores a esta marca se descartan
        self._last_total = -1  # total de la última ventana que pasó por VAD/STT
        self._desync = False   # último estado de desincronización informado al cliente
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
        self._since = self._audio.total_samples
        self._task = asyncio.create_task(self._run(), name="voice-loop")
        self._level_task = asyncio.create_task(self._level_loop(), name="voice-level")

    def reposition(self) -> None:
        """Tras un reposicionamiento: descarta el audio capturado hasta ahora.

        Las ventanas siguientes solo usan muestras posteriores. Si el cliente
        veía "buscando la posición", vuelve a `listening`: la posición nueva
        la eligió el usuario.
        """
        self._since = self._audio.total_samples
        if self._desync:
            self._desync = False
            self._emit(status_msg("listening", LISTENING_MESSAGE))

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
        samples, total = self._audio.read_window(self._settings.window_s,
                                                 since_sample=self._since)
        capture_ts = float(self._audio.last_callback_ts or 0.0)
        if total <= self._last_total:
            return  # sin audio nuevo desde la última ventana: el resultado sería el mismo
        if samples.size < int(MIN_WINDOW_S * SAMPLE_RATE):
            return  # todavía no hay 1 s posterior al último reposicionamiento
        matcher = host.matcher
        prompt = matcher.context_before(PROMPT_WORDS) if self._settings.prompt_context else None
        near_text = matcher.text_near()
        out = await asyncio.to_thread(self._infer, samples, prompt, near_text)
        if self._halted:
            return
        if out is None:  # motor ocupado por una inferencia huérfana: se salta el ciclo
            return
        self._last_total = total
        if host.generation != generation:
            log.debug("Resultado descartado: hubo un reposicionamiento durante la inferencia.")
            return
        if self._settings.debug and out.text:
            prefix = DROPPED_PREFIX.format(reason=out.dropped) if out.dropped else ""
            self._emit(transcript_msg(prefix + out.text))
        if out.dropped and out.text:
            log.debug("Transcripción descartada (%s): %r", out.dropped, out.text)
        match = self._cycle_runner.apply(out, matcher)  # misma generación: mismo matcher
        if match is None:
            return
        resync = bool(getattr(match, "resync", False))
        if match.advanced or resync:
            lag_ms = int((time.monotonic() - capture_ts) * 1000) if capture_ts else 0
            self._emit(scroll_msg(
                current_idx=match.new_idx, epoch=host.epoch, confidence=match.confidence,
                matched_word=match.matched_word, stt_ms=out.stt_ms, lag_ms=lag_ms,
                resync=resync))
        self._update_desync(bool(getattr(match, "desync", False)) and not resync)

    def _update_desync(self, desync: bool) -> None:
        """Informa solo las transiciones: `desync` al entrar y `listening` al salir."""
        if desync == self._desync:
            return
        self._desync = desync
        if desync:
            log.info("Matcher desincronizado: se busca la posición en el guion.")
            self._emit(status_msg("desync", DESYNC_MESSAGE))
        else:
            log.info("Matcher sincronizado de nuevo.")
            self._emit(status_msg("listening", LISTENING_MESSAGE))

    def _infer(self, window: np.ndarray, prompt: str | None, near_text: str) -> CycleOutput | None:
        """Hilo de trabajo: VAD y STT, o None si otra inferencia sigue en curso."""
        if not self._infer_lock.acquire(blocking=False):
            return None
        try:
            return self._cycle_runner.transcribe(window, prompt=prompt, near_text=near_text)
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
