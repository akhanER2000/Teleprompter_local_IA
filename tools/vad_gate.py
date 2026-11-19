"""Puerta de voz: decide si hubo voz en el último tramo de una ventana de audio.

Reutiliza Silero VAD tal como lo trae faster-whisper (`faster_whisper.vad`):
el modelo ONNX viene dentro del paquete y corre en CPU con onnxruntime, sin
GPU y sin red. El bucle de voz lo consulta antes de cada inferencia para no
transcribir silencio ni ruido (docs/diseno_v2.md, sección 8).

Coste medido (2026-10-09, CPU del equipo de desarrollo, un hilo de
onnxruntime): unos 3 ms por ventana de 3 s y unos 90 ms la primera carga.
"""
from __future__ import annotations

import logging
import math
import threading

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps, get_vad_model

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # el modelo de faster-whisper analiza tramas de 512 muestras a 16 kHz

# Opciones de Silero para ventanas cortas. Los valores por defecto de
# faster-whisper (2 s de silencio mínimo y 400 ms de margen) están pensados
# para cortar archivos largos y aquí harían que la voz "se arrastre" hasta
# un segundo después de terminar.
MIN_SPEECH_MS = 100   # tramos de voz más cortos se descartan (clics, golpes)
MIN_SILENCE_MS = 100  # silencio que cierra un tramo de voz
SPEECH_PAD_MS = 30    # margen que se agrega a cada lado de un tramo

# Audio anterior a la cola que también se analiza: el modelo es recurrente y
# necesita algo de contexto, pero así el coste no crece con la ventana.
CONTEXT_S = 2.0

_FRAME_SAMPLES = 512  # una trama de Silero (32 ms); con menos no hay nada que decidir

_load_lock = threading.Lock()
_model_ready = False


def _ensure_model() -> None:
    """Carga Silero una sola vez por proceso, aunque lo pidan varios hilos.

    `get_vad_model` de faster-whisper ya guarda el modelo con `lru_cache`, pero
    dos hilos que llegan a la vez antes de la primera carga crearían dos
    sesiones de onnxruntime; el candado lo evita.
    """
    global _model_ready
    if _model_ready:
        return
    with _load_lock:
        if not _model_ready:
            get_vad_model()
            _model_ready = True
            logger.debug("Silero VAD cargado.")


class VadGate:
    """Detector de voz sobre ventanas de audio float32 mono a 16 kHz.

    `threshold` es la probabilidad de voz de Silero a partir de la cual una
    trama cuenta como voz (`TP_VAD_THRESHOLD`). El modelo se carga en la
    primera consulta (o con `load()`); si onnxruntime no está instalado, esa
    consulta lanza `RuntimeError`.
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = SAMPLE_RATE) -> None:
        threshold = float(threshold)
        if not 0.0 < threshold < 1.0:
            raise ValueError(f"threshold debe estar entre 0 y 1 (recibido {threshold!r})")
        if sample_rate != SAMPLE_RATE:
            raise ValueError(
                f"Silero VAD de faster-whisper solo admite {SAMPLE_RATE} Hz "
                f"(recibido {sample_rate!r})")
        self.threshold = threshold
        self.sample_rate = int(sample_rate)
        self._options = VadOptions(
            threshold=threshold,
            min_speech_duration_ms=MIN_SPEECH_MS,
            min_silence_duration_ms=MIN_SILENCE_MS,
            speech_pad_ms=SPEECH_PAD_MS,
        )

    def load(self) -> None:
        """Carga el modelo por adelantado (si no, se carga en la primera consulta)."""
        _ensure_model()

    def has_speech(self, samples: np.ndarray, tail_s: float = 1.0) -> bool:
        """True si algún tramo de voz toca los últimos `tail_s` segundos de la ventana.

        Se analiza la cola más `CONTEXT_S` segundos previos (o la ventana
        entera si es más corta). Una ventana de menos de 32 ms da False.
        """
        tail_s = float(tail_s)
        if not tail_s > 0 or math.isinf(tail_s):
            raise ValueError(f"tail_s debe ser un número positivo (recibido {tail_s!r})")
        audio = self._prepare(samples)
        if audio.size < _FRAME_SAMPLES:
            return False
        tail = min(audio.size, int(round(tail_s * self.sample_rate)))
        start = max(0, audio.size - tail - int(round(CONTEXT_S * self.sample_rate)))
        chunk = audio[start:]
        tail_start = chunk.size - tail
        return any(seg["end"] > tail_start for seg in self._speech_segments(chunk))

    def speech_ratio(self, samples: np.ndarray) -> float:
        """Fracción de la ventana (0..1) cubierta por tramos de voz."""
        audio = self._prepare(samples)
        if audio.size < _FRAME_SAMPLES:
            return 0.0
        covered = sum(
            max(0, min(seg["end"], audio.size) - max(seg["start"], 0))
            for seg in self._speech_segments(audio)
        )
        return min(1.0, covered / audio.size)

    # ------------------------------------------------------------ interno

    @staticmethod
    def _prepare(samples: np.ndarray) -> np.ndarray:
        """float32 contiguo de una dimensión, sin NaN ni infinitos."""
        audio = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        if audio.size and not np.isfinite(audio).all():
            audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)
        return audio

    def _speech_segments(self, audio: np.ndarray) -> list[dict]:
        """Tramos de voz de Silero como dicts con `start` y `end` en muestras."""
        _ensure_model()
        return get_speech_timestamps(audio, self._options, sampling_rate=self.sample_rate)
