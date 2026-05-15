"""Captura de audio en streaming. Productor asíncrono de chunks de 500 ms."""
import asyncio
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 8000  # 500 ms
QUEUE_MAX = 4
RMS_THRESHOLD = 0.005


@dataclass
class AudioChunk:
    samples: np.ndarray
    timestamp_ms: int


class AudioCapture:
    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self.queue = queue
        self.loop = loop
        self.stream: sd.InputStream | None = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._t0 = time.time()

    def _callback(self, indata, frames, time_info, status):
        if status:
            pass  # underflow/overflow, no fatal
        samples = indata[:, 0].copy()
        self._buffer = np.concatenate([self._buffer, samples])
        while len(self._buffer) >= CHUNK_SAMPLES:
            chunk_samples = self._buffer[:CHUNK_SAMPLES]
            self._buffer = self._buffer[CHUNK_SAMPLES:]
            rms = float(np.sqrt(np.mean(chunk_samples ** 2)))
            if rms < RMS_THRESHOLD:
                continue  # silencio, no encolar
            ts_ms = int((time.time() - self._t0) * 1000)
            chunk = AudioChunk(samples=chunk_samples, timestamp_ms=ts_ms)
            try:
                self.loop.call_soon_threadsafe(self._push, chunk)
            except RuntimeError:
                pass  # loop cerrado

    def _push(self, chunk: AudioChunk):
        if self.queue.full():
            try:
                self.queue.get_nowait()  # descarta el más antiguo
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(chunk)

    def start(self):
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=1600,  # 100 ms
            callback=self._callback,
        )
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
