"""Motor STT basado en faster-whisper. Consume chunks y devuelve texto."""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass

import numpy as np

from faster_whisper import WhisperModel


@dataclass
class Transcription:
    text: str
    latency_ms: int
    timestamp_ms: int


class STTEngine:
    def __init__(self, model_size: str = "medium", device: str = "cuda",
                 compute_type: str = "float16", language: str = "es"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.model: WhisperModel | None = None

    def load(self):
        self.model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
        )

    def transcribe_sync(self, samples: np.ndarray, timestamp_ms: int) -> Transcription:
        assert self.model is not None, "Modelo no cargado"
        t0 = time.time()
        segments, _info = self.model.transcribe(
            samples,
            language=self.language,
            vad_filter=True,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
        )
        text = " ".join(seg.text for seg in segments).strip()
        latency_ms = int((time.time() - t0) * 1000)
        return Transcription(text=text, latency_ms=latency_ms, timestamp_ms=timestamp_ms)

    async def transcribe(self, samples: np.ndarray, timestamp_ms: int) -> Transcription:
        # Offload a thread para no bloquear el loop async.
        return await asyncio.to_thread(self.transcribe_sync, samples, timestamp_ms)
