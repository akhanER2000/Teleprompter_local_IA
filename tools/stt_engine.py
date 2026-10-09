"""Motor STT con faster-whisper: carga el modelo y transcribe una ventana de audio.

API síncrona (docs/diseno_v2.md, sección 9): quien llama la ejecuta en un
hilo. Nunca descarga nada: el modelo tiene que estar ya en el disco.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from glob import escape, glob

import numpy as np
from faster_whisper import WhisperModel, download_model
from huggingface_hub.errors import LocalEntryNotFoundError

from tools.cuda_dlls import register_cuda_dlls

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CPU_FALLBACK_COMPUTE = "int8"
WARMUP_S = 1.0
# Lo que CTranslate2 4.x admite en una CPU x86 si no se puede consultar.
_CPU_COMPUTE_TYPES_FALLBACK = frozenset({"int8", "int8_float32", "float32", "int16"})
# Tipos especiales que CTranslate2 resuelve solo para cada dispositivo.
_AUTO_COMPUTE_TYPES = frozenset({"default", "auto"})
# Archivos sin los que el modelo no carga (o tokenizers intentaría bajarlos).
_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")


class STTError(Exception):
    """Error del motor STT con un código del protocolo (MODEL_MISSING, MODEL_ERROR...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SegmentInfo:
    text: str
    no_speech_prob: float
    avg_logprob: float
    compression_ratio: float


@dataclass
class Transcription:
    text: str
    segments: list[SegmentInfo] = field(default_factory=list)
    stt_ms: int = 0


def _missing_model_message(model_size: str, detail: str = "") -> str:
    msg = (
        f"No se encontró el modelo de Whisper '{model_size}' en el disco y la app no "
        "descarga nada mientras se ejecuta. Descárgalo una vez, con conexión a internet, "
        "ejecutando scripts\\install.bat o con: .venv312\\Scripts\\python.exe -c "
        f"\"from faster_whisper import download_model; download_model('{model_size}')\""
    )
    return f"{msg} ({detail})" if detail else msg


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}".rstrip(": ")


def cpu_compute_types() -> frozenset[str]:
    """Tipos de cómputo que CTranslate2 admite en esta CPU."""
    try:
        import ctranslate2  # dependencia de faster-whisper

        return frozenset(ctranslate2.get_supported_compute_types("cpu"))
    except Exception as exc:  # solo orienta la elección: nunca impide cargar
        logger.debug("No se pudieron consultar los tipos de cómputo de la CPU: %s", exc)
        return _CPU_COMPUTE_TYPES_FALLBACK


class STTEngine:
    """Whisper (faster-whisper) con idioma fijo y decodificación voraz."""

    def __init__(self, model_size: str = "medium", device: str = "cuda",
                 compute_type: str = "float16", language: str = "es") -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.device_in_use: str = device          # "cpu" tras un fallback
        self.compute_type_in_use: str = compute_type
        self.fallback_reason: str | None = None   # motivo del fallback a CPU
        self.model_path: str | None = None
        self.model: WhisperModel | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self.model is not None

    # ------------------------------------------------------------ carga

    def load(self) -> None:
        """Carga el modelo desde el disco y hace el warmup.

        Lanza `STTError` con `MODEL_MISSING` si el modelo no está descargado o
        `MODEL_ERROR` si no carga. Si CUDA falla, reintenta en CPU con int8 y
        deja el motivo en `fallback_reason`. En CPU, un tipo de cómputo que la
        CPU no admite (el `float16` por defecto) se cambia por int8.
        """
        register_cuda_dlls()
        self.fallback_reason = None
        path = self._resolve_model_path()
        try:
            self._load_on(path, self.device, self._compute_type_for(self.device))
        except STTError:
            raise
        except Exception as exc:
            if self.device == "cpu":
                raise STTError(
                    "MODEL_ERROR",
                    f"No se pudo cargar el modelo '{self.model_size}' en CPU: {_short(exc)}",
                ) from exc
            self._fallback_to_cpu(path, exc)

    def _compute_type_for(self, device: str) -> str:
        """El tipo configurado, o int8 si es CPU y no lo admite (TP_DEVICE=cpu con float16)."""
        compute = self.compute_type
        if device != "cpu" or compute in _AUTO_COMPUTE_TYPES:
            return compute
        supported = cpu_compute_types()
        if compute in supported:
            return compute
        logger.warning("La CPU no admite el tipo de cómputo '%s' (admite: %s); se usa '%s'.",
                       compute, ", ".join(sorted(supported)), CPU_FALLBACK_COMPUTE)
        return CPU_FALLBACK_COMPUTE

    def _fallback_to_cpu(self, path: str, cause: Exception) -> None:
        self.fallback_reason = f"{self.device} falló al cargar: {_short(cause)}"
        logger.warning("Whisper no cargó en %s (%s); se reintenta en CPU con %s.",
                       self.device, _short(cause), CPU_FALLBACK_COMPUTE)
        try:
            self._load_on(path, "cpu", CPU_FALLBACK_COMPUTE)
        except STTError:
            raise
        except Exception as exc:
            raise STTError(
                "MODEL_ERROR",
                f"No se pudo cargar el modelo '{self.model_size}' ni en {self.device} "
                f"({_short(cause)}) ni en CPU ({_short(exc)}).",
            ) from exc

    def _resolve_model_path(self) -> str:
        """Ruta local del modelo, sin red. Falta algo: `STTError(MODEL_MISSING)`."""
        if os.path.isdir(self.model_size):
            path = self.model_size
        else:
            try:
                path = download_model(self.model_size, local_files_only=True)
            except LocalEntryNotFoundError as exc:
                raise STTError("MODEL_MISSING", _missing_model_message(self.model_size)) from exc
            except ValueError as exc:  # nombre de modelo desconocido
                raise STTError("MODEL_ERROR", f"Modelo de Whisper no válido: {exc}") from exc
        missing = [f for f in _REQUIRED_FILES if not os.path.isfile(os.path.join(path, f))]
        if not glob(os.path.join(escape(path), "vocabulary.*")):
            missing.append("vocabulary.*")
        if missing:
            raise STTError("MODEL_MISSING", _missing_model_message(
                self.model_size, f"faltan {', '.join(missing)} en {path}"))
        self.model_path = path
        return path

    def _load_on(self, path: str, device: str, compute_type: str) -> None:
        t0 = time.perf_counter()
        try:
            model = WhisperModel(path, device=device, compute_type=compute_type,
                                 local_files_only=True)
        except LocalEntryNotFoundError as exc:
            raise STTError("MODEL_MISSING", _missing_model_message(self.model_size)) from exc
        load_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        try:
            self._run(model, np.zeros(int(WARMUP_S * SAMPLE_RATE), dtype=np.float32), None)
        except BaseException:
            del model  # el traceback no debe retener el modelo (y su VRAM)
            raise
        warmup_ms = (time.perf_counter() - t1) * 1000
        self.model = model
        self.device_in_use = device
        self.compute_type_in_use = compute_type
        logger.info("Whisper '%s' listo en %s/%s (carga %.0f ms, warmup %.0f ms).",
                    self.model_size, device, compute_type, load_ms, warmup_ms)

    # ------------------------------------------------------------ inferencia

    def _run(self, model: WhisperModel, samples: np.ndarray,
             initial_prompt: str | None) -> list:
        segments, _info = model.transcribe(
            samples,
            language=self.language,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,
            initial_prompt=initial_prompt,
        )
        return list(segments)  # el generador es perezoso: aquí corre la inferencia

    def transcribe(self, samples: np.ndarray, initial_prompt: str | None = None) -> Transcription:
        """Transcribe una ventana de audio float32 a 16 kHz mono."""
        model = self.model
        if model is None:
            raise STTError("MODEL_NOT_READY", "El modelo de voz todavía no está cargado.")
        audio = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return Transcription(text="", segments=[], stt_ms=0)
        with self._lock:  # una inferencia a la vez; stt_ms no cuenta la espera
            t0 = time.perf_counter()
            try:
                raw = self._run(model, audio, initial_prompt or None)
            except Exception as exc:
                raise STTError("MODEL_ERROR", f"Falló la transcripción: {_short(exc)}") from exc
            stt_ms = int(round((time.perf_counter() - t0) * 1000))
        segments = [
            SegmentInfo(
                text=s.text.strip(),
                no_speech_prob=float(s.no_speech_prob),
                avg_logprob=float(s.avg_logprob),
                compression_ratio=float(s.compression_ratio),
            )
            for s in raw
        ]
        text = " ".join(s.text for s in segments if s.text)
        return Transcription(text=text, segments=segments, stt_ms=stt_ms)
