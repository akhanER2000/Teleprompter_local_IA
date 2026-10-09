"""Fuente de audio que reproduce un archivo en tiempo real (pruebas y demos).

Misma interfaz que `AudioCapture` (docs/diseno_v2.md, sección 8): decodifica
el archivo completo a 16 kHz mono al primer `start()` y lo vuelca al buffer
circular en bloques de ~100 ms al ritmo del reloj. Al terminar el archivo
sigue entregando silencio, como un micrófono en una sala callada.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from tools.audio_capture import (
    BLOCK_S,
    LEVEL_FLOOR_DB,
    SAMPLE_RATE,
    AudioCaptureError,
    RingBuffer,
    buffer_capacity,
    rms_dbfs,
)

logger = logging.getLogger(__name__)

MAX_BACKLOG_S = 1.0  # si el hilo se atrasa más que esto, no recupera en ráfaga
_JOIN_TIMEOUT_S = 2.0


def decode_file(path: str | os.PathLike, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decodifica un archivo de audio completo a float32 mono a `sample_rate`.

    Equivale a `faster_whisper.decode_audio`, que en la versión 1.2.1 pasa
    `metadata_errors` a `av.open` y falla con PyAV 19, donde ese argumento ya
    no existe.
    """
    import av  # dependencia de faster-whisper

    resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
    chunks: list[np.ndarray] = []
    with av.open(os.fspath(path), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("el archivo no tiene pistas de audio")
        frames = container.decode(audio=0)
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except av.error.InvalidDataError:  # como faster-whisper: se ignora
                continue
            frame.pts = None  # sin comprobación de marcas de tiempo
            chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.ascontiguousarray(np.concatenate(chunks), dtype=np.float32)


class AudioFileSource:
    """Reproduce un archivo de audio como si fuera el micrófono.

    `speed` acelera el reloj (1.0 es tiempo real); `block_s` es la duración
    de cada bloque. `stop()` seguido de `start()` continúa donde quedó.
    """

    sample_rate: int = SAMPLE_RATE

    def __init__(self, path: str | os.PathLike, window_s: float = 3.0, *,
                 block_s: float = BLOCK_S, speed: float = 1.0) -> None:
        if not block_s > 0:
            raise ValueError(f"block_s debe ser positivo (recibido {block_s!r})")
        if not speed > 0:
            raise ValueError(f"speed debe ser positivo (recibido {speed!r})")
        self.path = os.fspath(path)
        self.window_s = float(window_s)
        self.block_s = float(block_s)
        self.speed = float(speed)
        self._ring = RingBuffer(buffer_capacity(self.window_s))
        self.level_db: float = LEVEL_FLOOR_DB
        self.last_callback_ts: float = 0.0
        self._audio: np.ndarray | None = None
        self._pos = 0  # muestras del archivo ya entregadas
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ interfaz

    @property
    def total_samples(self) -> int:
        return self._ring.total_samples

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def duration_s(self) -> float | None:
        """Duración del archivo, o None si aún no se decodificó."""
        return None if self._audio is None else self._audio.size / SAMPLE_RATE

    @property
    def position_s(self) -> float:
        """Segundos del archivo ya entregados (sin contar el silencio final)."""
        return self._pos / SAMPLE_RATE

    @property
    def finished(self) -> bool:
        """True cuando ya se entregó el archivo entero."""
        return self._audio is not None and self._pos >= self._audio.size

    def read_window(self, seconds: float, since_sample: int = 0) -> tuple[np.ndarray, int]:
        """(muestras de los últimos `seconds` posteriores a since_sample, total al leer)."""
        return self._ring.read_window(seconds, since_sample)

    def start(self) -> None:
        """Decodifica el archivo (solo la primera vez) y empieza a entregarlo.

        Lanza `AudioCaptureError` si el archivo no se puede leer.
        """
        with self._lock:
            if self._thread is not None:
                return
            if self._audio is None:
                self._audio = self._decode()
            self._stop_event = threading.Event()
            self.last_callback_ts = time.monotonic()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop_event,),
                name="audio-file-source", daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Detiene la entrega. Idempotente; la posición del archivo se conserva."""
        with self._lock:
            thread, self._thread = self._thread, None
            if thread is None:
                return
            self._stop_event.set()
            if thread is not threading.current_thread():
                thread.join(timeout=_JOIN_TIMEOUT_S)
                if thread.is_alive():
                    logger.warning("El hilo de %s no terminó a tiempo.", self.path)
            self.level_db = LEVEL_FLOOR_DB

    # ------------------------------------------------------------ interno

    def _decode(self) -> np.ndarray:
        try:
            audio = decode_file(self.path, SAMPLE_RATE)
        except Exception as exc:
            raise AudioCaptureError(
                f"No se pudo leer el archivo de audio '{self.path}': {exc}"
            ) from exc
        logger.info("Fuente de audio: %s (%.1f s).", self.path, audio.size / SAMPLE_RATE)
        return audio

    def _next_block(self, n: int) -> np.ndarray:
        chunk = self._audio[self._pos:self._pos + n]
        self._pos += chunk.size
        if chunk.size < n:  # fin del archivo: completa con silencio
            chunk = np.concatenate([chunk, np.zeros(n - chunk.size, dtype=np.float32)])
        return chunk

    def _run(self, stop_event: threading.Event) -> None:
        """Hilo productor: cada bloque se entrega cuando su duración ya pasó."""
        n = max(1, int(round(self.block_s * SAMPLE_RATE)))
        period = n / SAMPLE_RATE / self.speed
        next_t = time.monotonic()
        try:
            while not stop_event.is_set():
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    if stop_event.wait(delay):
                        break
                elif delay < -MAX_BACKLOG_S:
                    next_t = time.monotonic()
                block = self._next_block(n)
                self._ring.write(block)
                self.level_db = rms_dbfs(block)
                self.last_callback_ts = time.monotonic()
        except Exception:  # sin bloques nuevos, la vigilancia del bucle avisa (MIC_LOST)
            logger.exception("La fuente de audio %s se detuvo por un error.", self.path)
