"""Captura continua del micrófono a un buffer circular de 16 kHz mono.

Interfaz común con `AudioFileSource` (docs/diseno_v2.md, sección 8). Nunca
descarta audio por RMS: el nivel en dBFS solo alimenta el medidor.
"""
from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except OSError as _exc:  # PortAudio no disponible: el buffer sigue siendo usable
    sd = None
    _SD_IMPORT_ERROR: OSError | None = _exc
else:
    _SD_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK_S = 0.1            # duración de cada bloque del callback
MIN_BUFFER_S = 6.0       # capacidad mínima del buffer circular
LEVEL_FLOOR_DB = -120.0  # nivel de silencio digital
_LOG_EVERY = 100         # registra 1 de cada N incidencias repetidas


class AudioCaptureError(RuntimeError):
    """El dispositivo (o el archivo) de audio no se pudo abrir."""


def rms_dbfs(block: np.ndarray) -> float:
    """Nivel RMS de un bloque en dBFS, con piso en `LEVEL_FLOOR_DB`."""
    if block.size == 0:
        return LEVEL_FLOOR_DB
    rms = math.sqrt(float(np.mean(np.square(block, dtype=np.float64))))
    if rms <= 0.0:
        return LEVEL_FLOOR_DB
    return max(LEVEL_FLOOR_DB, 20.0 * math.log10(rms))


def buffer_capacity(window_s: float, sample_rate: int = SAMPLE_RATE) -> int:
    """Capacidad del buffer en muestras: max(2 * window_s, 6) segundos."""
    if not window_s > 0:
        raise ValueError(f"window_s debe ser positivo (recibido {window_s!r})")
    return int(round(max(2.0 * window_s, MIN_BUFFER_S) * sample_rate))


class RingBuffer:
    """Buffer circular float32 con contador global de muestras. Seguro entre hilos.

    La muestra con índice global `k` vive en la posición `k % capacity`; solo
    se conservan las últimas `capacity` muestras.
    """

    def __init__(self, capacity: int, sample_rate: int = SAMPLE_RATE) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity debe ser positiva (recibido {capacity!r})")
        self.capacity = int(capacity)
        self.sample_rate = int(sample_rate)
        self._data = np.zeros(self.capacity, dtype=np.float32)
        self._total = 0
        self._lock = threading.Lock()

    @property
    def total_samples(self) -> int:
        """Muestras escritas desde la creación (nunca baja)."""
        return self._total

    def write(self, samples: np.ndarray) -> int:
        """Agrega muestras (se copian) y devuelve el nuevo total."""
        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        n = block.size
        with self._lock:
            if n:
                m = min(n, self.capacity)  # de un bloque enorme solo cabe la cola
                pos = (self._total + n - m) % self.capacity
                first = min(m, self.capacity - pos)
                self._data[pos:pos + first] = block[n - m:n - m + first]
                self._data[:m - first] = block[n - m + first:]
                self._total += n
            return self._total

    def read_last(self, n_samples: int, since_sample: int = 0) -> tuple[np.ndarray, int]:
        """Últimas `n_samples` muestras con índice >= `since_sample`.

        Devuelve una copia y el total en el momento de leer, que es el índice
        siguiente a la última muestra devuelta.
        """
        with self._lock:
            total = self._total
            start = max(total - max(int(n_samples), 0), total - self.capacity,
                        int(since_sample), 0)
            if start >= total:
                return np.zeros(0, dtype=np.float32), total
            m = total - start
            pos = start % self.capacity
            first = min(m, self.capacity - pos)
            out = np.empty(m, dtype=np.float32)
            out[:first] = self._data[pos:pos + first]
            out[first:] = self._data[:m - first]
            return out, total

    def read_window(self, seconds: float, since_sample: int = 0) -> tuple[np.ndarray, int]:
        """Como `read_last`, con la duración en segundos."""
        return self.read_last(int(round(seconds * self.sample_rate)), since_sample)


class AudioCapture:
    """Micrófono a buffer circular de 16 kHz mono, con nivel en dBFS.

    Abre a 16 kHz; si el dispositivo rechaza esa tasa, abre a su tasa nativa y
    remuestrea en el callback con un `av.AudioResampler` persistente.
    """

    sample_rate: int = SAMPLE_RATE

    def __init__(self, device: int | None = None, window_s: float = 3.0) -> None:
        self.device = device
        self.window_s = float(window_s)
        self._ring = RingBuffer(buffer_capacity(self.window_s))
        self.level_db: float = LEVEL_FLOOR_DB
        # Se fija también en start() para que la vigilancia tenga una referencia.
        self.last_callback_ts: float = 0.0
        self.input_samplerate: int | None = None  # tasa real del dispositivo abierto
        self.input_channels: int | None = None
        self.device_name: str | None = None
        self.overflow_count = 0
        self.callback_errors = 0
        self._resampler = None
        self._stream = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ interfaz

    @property
    def total_samples(self) -> int:
        return self._ring.total_samples

    @property
    def running(self) -> bool:
        return self._stream is not None

    @property
    def resampling(self) -> bool:
        return self._resampler is not None

    def read_window(self, seconds: float, since_sample: int = 0) -> tuple[np.ndarray, int]:
        """(muestras de los últimos `seconds` posteriores a since_sample, total al leer)."""
        return self._ring.read_window(seconds, since_sample)

    def start(self) -> None:
        """Abre el dispositivo y empieza a capturar. Lanza `AudioCaptureError` si falla."""
        with self._lock:
            if self._stream is not None:
                return
            if sd is None:
                raise AudioCaptureError(
                    f"No se pudo cargar PortAudio (sounddevice): {_SD_IMPORT_ERROR}"
                ) from _SD_IMPORT_ERROR
            stream = self._open()
            try:
                stream.start()
            except Exception as exc:
                self._close_quietly(stream)
                self._resampler = None
                raise AudioCaptureError(
                    f"No se pudo iniciar la captura del micrófono ({self._label()}): {exc}"
                ) from exc
            self._stream = stream
            self.last_callback_ts = time.monotonic()
            logger.info("Micrófono abierto: %s a %d Hz, %d canal(es)%s.", self._label(),
                        self.input_samplerate, self.input_channels,
                        ", remuestreo a 16 kHz" if self._resampler is not None else "")

    def stop(self) -> None:
        """Detiene y cierra el dispositivo. Idempotente."""
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is None:
                return
            try:
                stream.stop()
            except Exception as exc:  # el dispositivo pudo desaparecer
                logger.warning("Error al detener el micrófono: %s", exc)
            self._close_quietly(stream)
            self._resampler = None
            self.level_db = LEVEL_FLOOR_DB

    # ------------------------------------------------------------ apertura

    def _label(self) -> str:
        if self.device_name:
            return f"'{self.device_name}'"
        return "predeterminado" if self.device is None else f"#{self.device}"

    def _prepare(self, samplerate: int, channels: int = 1) -> None:
        """Ajusta la tasa de entrada y crea el remuestreador si hace falta."""
        self.input_samplerate = int(samplerate)
        self.input_channels = int(channels)
        if self.input_samplerate == SAMPLE_RATE:
            self._resampler = None
        else:
            import av  # dependencia de faster-whisper

            self._resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

    def _try_open(self, samplerate: int, channels: int, errors: list[tuple[str, Exception]]):
        """Abre el stream o devuelve None y anota el error en `errors`."""
        self._prepare(samplerate, channels)
        try:
            return sd.InputStream(
                samplerate=samplerate,
                channels=channels,
                dtype="float32",
                blocksize=int(round(samplerate * BLOCK_S)),
                device=self.device,
                callback=self._callback,
            )
        except (sd.PortAudioError, ValueError) as exc:
            errors.append((f"{samplerate} Hz y {channels} canal(es)", exc))
            return None

    def _open(self):
        """Intenta 16 kHz mono; si falla, la tasa nativa en mono y luego en estéreo."""
        errors: list[tuple[str, Exception]] = []
        stream = self._try_open(SAMPLE_RATE, 1, errors)
        if stream is None:
            try:
                info = sd.query_devices(self.device, "input")
            except (sd.PortAudioError, ValueError) as exc:
                errors.append(("consulta del dispositivo", exc))
                info = None
            if info is not None:
                self.device_name = info["name"]
                native = int(round(float(info["default_samplerate"])))
                if native != SAMPLE_RATE:
                    logger.info("El micrófono rechaza 16 kHz; se abre a %d Hz y se remuestrea.",
                                native)
                    stream = self._try_open(native, 1, errors)
                if stream is None and info["max_input_channels"] >= 2:
                    stream = self._try_open(native, 2, errors)
        if stream is None:
            self._resampler = None
            detail = "; ".join(f"{what}: {exc}" for what, exc in errors)
            raise AudioCaptureError(
                f"No se pudo abrir el micrófono ({self._label()}): {detail}"
            ) from errors[-1][1]
        try:
            self.device_name = sd.query_devices(stream.device)["name"]
        except Exception:  # solo informativo
            pass
        return stream

    @staticmethod
    def _close_quietly(stream) -> None:
        try:
            stream.close()
        except Exception as exc:
            logger.warning("Error al cerrar el micrófono: %s", exc)

    # ------------------------------------------------------------ callback

    def _callback(self, indata, frames, time_info, status) -> None:
        """Hilo de PortAudio: guarda el bloque y actualiza nivel y marca de tiempo."""
        try:
            if status:
                self._note_status(status)
            if indata.ndim == 2:
                block = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
            else:
                block = indata
            block = np.asarray(block, dtype=np.float32)
            self.level_db = rms_dbfs(block)
            resampler = self._resampler  # stop() puede soltarlo mientras tanto
            if resampler is not None:
                block = self._resample(resampler, block)
            self._ring.write(block)
            self.last_callback_ts = time.monotonic()
        except Exception:  # una excepción aquí abortaría el stream
            self.callback_errors += 1
            if self.callback_errors % _LOG_EVERY == 1:
                logger.exception("Error en el callback de audio (%d en total).",
                                 self.callback_errors)

    def _note_status(self, status) -> None:
        if getattr(status, "input_overflow", False):
            self.overflow_count += 1
            if self.overflow_count % _LOG_EVERY == 1:
                logger.warning("Desbordamiento de la entrada de audio (%d en total).",
                               self.overflow_count)
        else:
            logger.debug("Estado del callback de audio: %s", status)

    def _resample(self, resampler, block: np.ndarray) -> np.ndarray:
        import av

        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(block, dtype=np.float32).reshape(1, -1),
            format="flt", layout="mono",
        )
        frame.sample_rate = self.input_samplerate
        out = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return out[0] if len(out) == 1 else np.concatenate(out)


def list_input_devices() -> list[dict]:
    """Dispositivos con entrada: {index, name, hostapi, default_samplerate, is_default}.

    En Windows cada micrófono aparece una vez por API de audio (MME,
    DirectSound, WASAPI, WDM-KS); `hostapi` lleva el nombre de la API para
    distinguirlos. `is_default` marca el predeterminado de PortAudio.
    """
    if sd is None:
        raise AudioCaptureError(f"No se pudo cargar PortAudio (sounddevice): {_SD_IMPORT_ERROR}")
    hostapis = sd.query_hostapis()
    try:
        default_index = sd.query_devices(kind="input")["index"]
    except (sd.PortAudioError, ValueError):  # no hay entrada predeterminada
        default_index = None
    devices = []
    for info in sd.query_devices():
        if info["max_input_channels"] < 1:
            continue
        api = info["hostapi"]
        devices.append({
            "index": int(info["index"]),
            "name": info["name"],
            "hostapi": hostapis[api]["name"] if 0 <= api < len(hostapis) else str(api),
            "default_samplerate": float(info["default_samplerate"]),
            "is_default": info["index"] == default_index,
        })
    return devices
