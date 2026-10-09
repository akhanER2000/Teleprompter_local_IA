"""STT y audio falsos para pruebas y desarrollo sin GPU ni micrófono (`TP_FAKE_STT=1`).

Implementan las interfaces de `tools/stt_engine.py` (docs/diseno_v2.md,
sección 9) y de `tools/audio_capture.py` (sección 8) sin importar
faster-whisper ni sounddevice. Ninguna espera dura más de `MAX_WAIT_S`.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger("teleprompter.fakes")

SAMPLE_RATE = 16000
LEVEL_FLOOR_DB = -120.0
MAX_WAIT_S = 5.0


# ---------------------------------------------------------------- STT

@dataclass
class SegmentInfo:
    text: str
    no_speech_prob: float = 0.0
    avg_logprob: float = -0.2
    compression_ratio: float = 1.2


@dataclass
class Transcription:
    text: str
    segments: list[SegmentInfo] = field(default_factory=list)
    stt_ms: int = 0


class FakeModelError(RuntimeError):
    """Error de carga con `code` (MODEL_MISSING o MODEL_ERROR), como el motor real."""

    def __init__(self, message: str, code: str = "MODEL_ERROR") -> None:
        super().__init__(message)
        self.code = code


class FakeSTTEngine:
    """Motor STT falso: devuelve transcripciones programadas con `push`.

    - `load()` puede ser lento (`load_delay_s`), esperar a `load_gate` o
      fallar (`load_error`); con `cuda_fallback` simula el paso a CPU.
    - `transcribe()` saca la siguiente transcripción de una cola segura entre
      hilos y devuelve "" si está vacía. Tras `hold()`, las llamadas que sacan
      una transcripción no vacía esperan a `release()` (inferencia lenta).
    """

    def __init__(self, model_size: str = "fake", device: str = "cpu",
                 compute_type: str = "int8", language: str = "es", *,
                 load_delay_s: float = 0.0, load_error: BaseException | None = None,
                 load_gate: threading.Event | None = None, cuda_fallback: bool = False,
                 transcribe_delay_s: float = 0.0,
                 transcribe_error: BaseException | None = None) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.load_delay_s = load_delay_s
        self.load_error = load_error
        self.load_gate = load_gate
        self.cuda_fallback = cuda_fallback
        self.transcribe_delay_s = transcribe_delay_s
        self.transcribe_error = transcribe_error
        self.device_in_use = device
        self.fallback_reason: str | None = None
        self.loaded = False
        self.load_calls = 0
        self.calls = 0
        self.last_prompt: str | None = None
        self.last_samples = 0
        self._queue: queue.Queue[str] = queue.Queue()
        self._gate = threading.Event()
        self._gate.set()  # abierto: no bloquea
        self._cond = threading.Condition()
        self._holding: str | None = None

    def load(self) -> None:
        self.load_calls += 1
        if self.load_gate is not None and not self.load_gate.wait(MAX_WAIT_S):
            log.warning("FakeSTTEngine.load: se agotó la espera de load_gate.")
        if self.load_delay_s > 0:
            time.sleep(min(self.load_delay_s, MAX_WAIT_S))
        if self.load_error is not None:
            raise self.load_error
        if self.cuda_fallback and self.device == "cuda":
            self.device_in_use = "cpu"
            self.fallback_reason = "CUDA simulada no disponible"
        self.loaded = True

    # ------------------------------------------------------------ control

    def push(self, *texts: str) -> None:
        """Programa transcripciones; cada llamada a transcribe() saca una."""
        for text in texts:
            self._queue.put(text)

    def pending(self) -> int:
        return self._queue.qsize()

    def hold(self) -> None:
        """Las próximas transcripciones no vacías esperan a release()."""
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def wait_holding(self, text: str | None = None, timeout: float = MAX_WAIT_S) -> bool:
        """Espera a que una llamada quede retenida (con `text`, si se indica)."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._holding is not None and (text is None or self._holding == text),
                timeout=min(timeout, MAX_WAIT_S))

    def wait_calls(self, n: int, timeout: float = MAX_WAIT_S) -> bool:
        """Espera a que transcribe() se haya llamado al menos `n` veces."""
        with self._cond:
            return self._cond.wait_for(lambda: self.calls >= n, timeout=min(timeout, MAX_WAIT_S))

    # ------------------------------------------------------------ interfaz

    def transcribe(self, samples: np.ndarray, initial_prompt: str | None = None) -> Transcription:
        if not self.loaded:
            raise RuntimeError("Modelo no cargado")
        t0 = time.perf_counter()
        try:
            text = self._queue.get_nowait()
        except queue.Empty:
            text = ""
        with self._cond:
            self.calls += 1
            self.last_prompt = initial_prompt
            self.last_samples = int(np.asarray(samples).size)
            self._cond.notify_all()
        if text and not self._gate.is_set():
            with self._cond:
                self._holding = text
                self._cond.notify_all()
            try:
                if not self._gate.wait(MAX_WAIT_S):
                    log.warning("FakeSTTEngine.transcribe: se agotó la espera de release().")
            finally:
                with self._cond:
                    self._holding = None
                    self._cond.notify_all()
        if self.transcribe_delay_s > 0:
            time.sleep(min(self.transcribe_delay_s, MAX_WAIT_S))
        if self.transcribe_error is not None:
            raise self.transcribe_error
        stt_ms = int((time.perf_counter() - t0) * 1000)
        return Transcription(text=text, segments=[SegmentInfo(text)] if text else [], stt_ms=stt_ms)


# ---------------------------------------------------------------- audio

def _rms_dbfs(block: np.ndarray) -> float:
    if block.size == 0:
        return LEVEL_FLOOR_DB
    rms = math.sqrt(float(np.mean(np.square(block, dtype=np.float64))))
    return LEVEL_FLOOR_DB if rms <= 0.0 else max(LEVEL_FLOOR_DB, 20.0 * math.log10(rms))


class FakeAudioSource:
    """Fuente de audio falsa con la interfaz de `AudioCapture`.

    Genera un tono (no silencioso) a 16 kHz en tiempo real desde un hilo
    (`realtime=True`) o bajo demanda con `feed()`. `start_error` hace que
    start() lance (MIC_ERROR) y `stall()` deja de producir bloques (MIC_LOST).
    """

    sample_rate: int = SAMPLE_RATE

    def __init__(self, *, start_error: BaseException | None = None, realtime: bool = True,
                 block_s: float = 0.1, amplitude: float = 0.1, freq_hz: float = 220.0,
                 buffer_s: float = 6.0) -> None:
        self.start_error = start_error
        self.realtime = realtime
        self.block_s = block_s
        self.amplitude = amplitude
        self.freq_hz = freq_hz
        self.level_db: float = LEVEL_FLOOR_DB
        self.last_callback_ts: float = 0.0
        self.start_calls = 0
        self.stop_calls = 0
        self._buf = np.zeros(max(1, int(buffer_s * SAMPLE_RATE)), dtype=np.float32)
        self._total = 0
        self._lock = threading.Lock()
        self._running = False
        self._stalled = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def total_samples(self) -> int:
        return self._total

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        if self._running:
            return
        self._running = True
        self.last_callback_ts = time.monotonic()
        if self.realtime:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="fake-audio", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self.stop_calls += 1
        self._running = False
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=MAX_WAIT_S)
        self.level_db = LEVEL_FLOOR_DB

    def stall(self) -> None:
        """Deja de producir bloques sin detenerse (micrófono desconectado)."""
        self._stalled.set()

    def resume(self) -> None:
        self._stalled.clear()

    def feed(self, seconds: float | None = None, samples: np.ndarray | None = None) -> int:
        """Escribe un bloque (tono sintético o `samples`) como lo haría el callback."""
        if samples is None:
            n = max(1, int(round((seconds if seconds is not None else self.block_s) * SAMPLE_RATE)))
            t = (np.arange(n, dtype=np.float64) + self._total) / SAMPLE_RATE
            samples = (self.amplitude * np.sin(2.0 * math.pi * self.freq_hz * t)).astype(np.float32)
        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            n = block.size
            cap = self._buf.size
            start = self._total
            if n > cap:  # de un bloque enorme solo cabe la cola
                start += n - cap
                block = block[-cap:]
            self._buf[(np.arange(block.size) + start) % cap] = block
            self._total += n
            total = self._total
        self.level_db = _rms_dbfs(block)
        self.last_callback_ts = time.monotonic()
        return total

    def read_window(self, seconds: float, since_sample: int = 0) -> tuple[np.ndarray, int]:
        with self._lock:
            total = self._total
            cap = self._buf.size
            start = max(total - int(round(seconds * SAMPLE_RATE)), total - cap, int(since_sample), 0)
            if start >= total:
                return np.zeros(0, dtype=np.float32), total
            return self._buf[np.arange(start, total) % cap], total

    def _run(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += self.block_s
            delay = next_t - time.monotonic()
            if delay < -1.0:  # el hilo se atrasó mucho: no recupera en ráfaga
                next_t = time.monotonic()
                delay = 0.0
            if self._stop.wait(max(0.0, delay)):
                break
            if not self._stalled.is_set():
                self.feed(self.block_s)
