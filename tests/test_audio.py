"""Buffer circular, captura y fuente de archivo (docs/diseno_v2.md, sección 8).

Nada abre un dispositivo real: el callback de la captura se llama a mano y el
stream de sounddevice se sustituye por uno falso.
"""
from __future__ import annotations

import math
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from tools import audio_capture
from tools.audio_capture import (
    LEVEL_FLOOR_DB,
    AudioCapture,
    AudioCaptureError,
    RingBuffer,
    buffer_capacity,
    list_input_devices,
    rms_dbfs,
)
from tools.audio_file_source import AudioFileSource

SR = 16000
needs_sd = pytest.mark.skipif(audio_capture.sd is None, reason="PortAudio no disponible")


def _wait_until(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("tiempo de espera agotado")
        time.sleep(0.005)


def _sine(freq: float, n: int, sr: int, amp: float = 0.5, start: int = 0) -> np.ndarray:
    t = (np.arange(n) + start) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _write_wav(path: Path, samples: np.ndarray, sr: int = SR, channels: int = 1) -> Path:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


# ---------------------------------------------------------------- RingBuffer

def test_ring_buffer_escritura_y_lectura_basica() -> None:
    rb = RingBuffer(10, sample_rate=10)
    assert rb.total_samples == 0
    data, total = rb.read_last(5)
    assert data.size == 0 and total == 0

    assert rb.write(np.arange(4, dtype=np.float32)) == 4
    data, total = rb.read_last(10)
    assert total == 4 and data.tolist() == [0, 1, 2, 3]
    data, _ = rb.read_last(2)
    assert data.tolist() == [2, 3]
    assert data.dtype == np.float32


def test_ring_buffer_vuelta_completa() -> None:
    rb = RingBuffer(10, sample_rate=10)
    rb.write(np.arange(0, 7))
    rb.write(np.arange(7, 15))  # cruza el final del arreglo interno
    assert rb.total_samples == 15
    data, total = rb.read_last(10)
    assert total == 15 and data.tolist() == list(range(5, 15))
    data, _ = rb.read_last(100)  # nunca más que la capacidad
    assert data.tolist() == list(range(5, 15))
    data, _ = rb.read_last(3)
    assert data.tolist() == [12, 13, 14]
    for k in range(15, 40, 3):  # muchas vueltas con bloques desalineados
        rb.write(np.arange(k, k + 3))
    data, total = rb.read_last(10)
    assert total == 42 and data.tolist() == list(range(32, 42))


def test_ring_buffer_bloque_mayor_que_la_capacidad() -> None:
    rb = RingBuffer(10, sample_rate=10)
    rb.write(np.arange(3))
    assert rb.write(np.arange(3, 28)) == 28
    data, total = rb.read_last(10)
    assert total == 28 and data.tolist() == list(range(18, 28))
    rb.write(np.arange(28, 31))
    assert rb.read_last(10)[0].tolist() == list(range(21, 31))


def test_ring_buffer_since_sample_filtra_y_devuelve_el_total() -> None:
    rb = RingBuffer(10, sample_rate=10)
    rb.write(np.arange(15))
    data, total = rb.read_last(10, since_sample=12)
    assert total == 15 and data.tolist() == [12, 13, 14]
    data, total = rb.read_last(2, since_sample=12)  # gana el más restrictivo
    assert data.tolist() == [13, 14]
    for since in (15, 20):
        data, total = rb.read_last(10, since_sample=since)
        assert data.size == 0 and total == 15
    data, _ = rb.read_last(10, since_sample=3)  # ya no está en el buffer
    assert data.tolist() == list(range(5, 15))
    data, _ = rb.read_last(4, since_sample=-7)
    assert data.tolist() == [11, 12, 13, 14]


def test_ring_buffer_read_window_en_segundos_y_copia() -> None:
    rb = RingBuffer(20, sample_rate=10)
    rb.write(np.arange(20))
    data, total = rb.read_window(0.5)
    assert total == 20 and data.tolist() == [15, 16, 17, 18, 19]
    data, _ = rb.read_window(1.0, since_sample=17)
    assert data.tolist() == [17, 18, 19]
    data[:] = -1  # la lectura es una copia
    assert rb.read_window(0.3)[0].tolist() == [17, 18, 19]


def test_ring_buffer_lecturas_concurrentes_coherentes() -> None:
    rb = RingBuffer(1000, sample_rate=SR)
    errors: list[str] = []
    done = threading.Event()

    def writer() -> None:
        for k in range(0, 50_000, 37):
            rb.write(np.arange(k, k + 37, dtype=np.float32))
        done.set()

    def reader() -> None:
        while not done.is_set():
            data, total = rb.read_last(500)
            expected = np.arange(total - data.size, total, dtype=np.float32)
            if not np.array_equal(data, expected):
                errors.append(f"lectura incoherente con total={total}")
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert not any(t.is_alive() for t in threads)
    assert not errors
    assert rb.total_samples == len(range(0, 50_000, 37)) * 37


def test_ring_buffer_capacidad_invalida() -> None:
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_buffer_capacity_minimo_seis_segundos() -> None:
    assert buffer_capacity(3.0) == 6 * SR
    assert buffer_capacity(1.0) == 6 * SR
    assert buffer_capacity(5.0) == 10 * SR
    for bad in (0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            buffer_capacity(bad)


def test_rms_dbfs() -> None:
    assert rms_dbfs(np.zeros(1600, np.float32)) == LEVEL_FLOOR_DB
    assert rms_dbfs(np.zeros(0, np.float32)) == LEVEL_FLOOR_DB
    assert rms_dbfs(np.full(100, 1e-9, np.float32)) == LEVEL_FLOOR_DB
    assert rms_dbfs(np.ones(100, np.float32)) == pytest.approx(0.0)
    assert rms_dbfs(_sine(1000, 1600, SR, amp=1.0)) == pytest.approx(-3.01, abs=0.05)


# ---------------------------------------------------------------- AudioCapture (callback)

def _flags(**set_flags):
    flags = audio_capture.sd.CallbackFlags()
    for name, value in set_flags.items():
        setattr(flags, name, value)
    return flags


def test_captura_inicial_sin_abrir_dispositivo() -> None:
    cap = AudioCapture(window_s=3.0)
    assert cap.sample_rate == SR
    assert not cap.running and not cap.resampling
    assert cap.total_samples == 0 and cap.level_db == LEVEL_FLOOR_DB
    assert cap._ring.capacity == 6 * SR
    assert AudioCapture(window_s=5.0)._ring.capacity == 10 * SR
    cap.stop()  # idempotente aunque nunca se haya abierto


@needs_sd
def test_callback_guarda_bloques_y_mide_nivel() -> None:
    cap = AudioCapture(window_s=1.0)
    tone = _sine(440, 1600, SR, amp=0.5)
    before = time.monotonic()
    cap._callback(tone.reshape(-1, 1), 1600, None, _flags())
    assert cap.total_samples == 1600
    assert cap.level_db == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.1)
    assert cap.last_callback_ts >= before
    data, total = cap.read_window(1.0)
    assert total == 1600 and np.array_equal(data, tone)

    # El silencio también se guarda: nunca se descarta audio por RMS.
    cap._callback(np.zeros((1600, 1), np.float32), 1600, None, _flags())
    assert cap.total_samples == 3200
    assert cap.level_db == LEVEL_FLOOR_DB
    data, total = cap.read_window(1.0, since_sample=1600)
    assert total == 3200 and data.size == 1600 and not data.any()


@needs_sd
def test_callback_cuenta_desbordamientos_sin_perder_el_bloque() -> None:
    cap = AudioCapture(window_s=1.0)
    cap._callback(np.full((1600, 1), 0.1, np.float32), 1600, None, _flags(input_overflow=True))
    cap._callback(np.full((1600, 1), 0.1, np.float32), 1600, None, _flags(input_overflow=True))
    assert cap.overflow_count == 2
    assert cap.total_samples == 3200


@needs_sd
def test_callback_mezcla_estereo_a_mono() -> None:
    cap = AudioCapture(window_s=1.0)
    stereo = np.stack([np.full(1600, 0.2), np.full(1600, 0.4)], axis=1).astype(np.float32)
    cap._callback(stereo, 1600, None, _flags())
    data, _ = cap.read_window(1.0)
    assert data.size == 1600 and np.allclose(data, 0.3)


def test_callback_no_propaga_excepciones() -> None:
    cap = AudioCapture(window_s=1.0)
    cap._callback(None, 0, None, None)  # datos inválidos
    assert cap.callback_errors == 1
    assert cap.total_samples == 0


@pytest.mark.parametrize("rate", [48000, 44100])
def test_remuestreo_a_16k(rate: int) -> None:
    cap = AudioCapture(window_s=3.0)
    cap._prepare(rate)
    assert cap.resampling and cap.input_samplerate == rate
    block = rate // 10
    for k in range(20):  # 2 s de audio a la tasa nativa
        tone = _sine(1000, block, rate, amp=0.5, start=k * block)
        cap._callback(tone.reshape(-1, 1), block, None, None)
    # 2 s a 16 kHz, menos el retardo del filtro.
    assert abs(cap.total_samples - 2 * SR) <= 64
    data, _ = cap.read_window(1.0)
    spectrum = np.abs(np.fft.rfft(data * np.hanning(data.size)))
    peak_hz = np.argmax(spectrum) * SR / data.size
    assert peak_hz == pytest.approx(1000, abs=2)
    assert rms_dbfs(data) == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.3)
    assert cap.level_db == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.3)


def test_prepare_a_16k_no_remuestrea() -> None:
    cap = AudioCapture()
    cap._prepare(48000)
    cap._prepare(SR)
    assert not cap.resampling


# ---------------------------------------------------------------- AudioCapture.start (stream falso)

class _FakeStream:
    """Sustituto de sounddevice.InputStream que nunca toca el hardware."""

    reject: set[tuple[int, int]] | None = set()  # None: rechaza todo
    fail_start = False
    instances: list[_FakeStream] = []
    attempts: list[tuple[int, int]] = []

    def __init__(self, *, samplerate, channels, dtype, blocksize, device, callback) -> None:
        type(self).attempts.append((samplerate, channels))
        if self.reject is None or (samplerate, channels) in self.reject:
            raise audio_capture.sd.PortAudioError("Invalid sample rate", -9997)
        self.samplerate, self.channels, self.dtype = samplerate, channels, dtype
        self.blocksize, self.callback = blocksize, callback
        self.device = 7 if device is None else device
        self.started = self.closed = False
        type(self).instances.append(self)

    def start(self) -> None:
        if self.fail_start:
            raise audio_capture.sd.PortAudioError("Device unavailable", -9985)
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sd(monkeypatch):
    if audio_capture.sd is None:
        pytest.skip("PortAudio no disponible")

    class Stream(_FakeStream):
        reject: set | None = set()
        fail_start = False
        instances: list = []
        attempts: list = []

    info = {"name": "Mic falso", "index": 7, "hostapi": 0, "max_input_channels": 2,
            "default_samplerate": 48000.0}

    def query_devices(device=None, kind=None):
        if info.get("broken"):
            raise audio_capture.sd.PortAudioError("Error querying device -1")
        return dict(info)

    monkeypatch.setattr(audio_capture.sd, "InputStream", Stream)
    monkeypatch.setattr(audio_capture.sd, "query_devices", query_devices)
    Stream.info = info
    return Stream


def test_start_abre_a_16k_y_stop_es_idempotente(fake_sd) -> None:
    cap = AudioCapture(device=None, window_s=3.0)
    cap.start()
    assert cap.running and not cap.resampling
    (stream,) = fake_sd.instances
    assert stream.started and (stream.samplerate, stream.channels) == (SR, 1)
    assert stream.dtype == "float32" and stream.blocksize == 1600
    assert cap.device_name == "Mic falso" and cap.input_samplerate == SR
    assert cap.last_callback_ts > 0
    cap.start()  # ya abierto: no abre otro
    assert len(fake_sd.instances) == 1

    cap.stop()
    assert stream.closed and not cap.running
    cap.stop()
    cap.start()  # se puede volver a abrir
    assert len(fake_sd.instances) == 2 and cap.running
    cap.stop()


def test_start_remuestrea_si_el_dispositivo_rechaza_16k(fake_sd) -> None:
    fake_sd.reject = {(SR, 1)}
    cap = AudioCapture(device=3, window_s=3.0)
    cap.start()
    (stream,) = fake_sd.instances
    assert fake_sd.attempts == [(SR, 1), (48000, 1)]
    assert (stream.samplerate, stream.channels, stream.blocksize) == (48000, 1, 4800)
    assert stream.device == 3
    assert cap.resampling and cap.input_samplerate == 48000
    for k in range(10):  # el callback real del stream remuestrea a 16 kHz
        tone = _sine(440, 4800, 48000, start=k * 4800)
        stream.callback(tone.reshape(-1, 1), 4800, None, _flags())
    assert abs(cap.total_samples - SR) <= 64
    cap.stop()
    assert stream.closed and not cap.resampling


def test_start_prueba_estereo_si_mono_falla(fake_sd) -> None:
    fake_sd.reject = {(SR, 1), (48000, 1)}
    cap = AudioCapture()
    cap.start()
    assert fake_sd.attempts == [(SR, 1), (48000, 1), (48000, 2)]
    assert cap.input_channels == 2 and cap.resampling
    cap.stop()


def test_start_falla_con_error_claro(fake_sd) -> None:
    fake_sd.reject = None
    cap = AudioCapture(device=5)
    with pytest.raises(AudioCaptureError, match="No se pudo abrir el micrófono") as info:
        cap.start()
    assert "48000 Hz" in str(info.value)
    assert isinstance(info.value.__cause__, audio_capture.sd.PortAudioError)
    assert not cap.running and not cap.resampling
    cap.stop()


def test_start_falla_si_no_hay_dispositivo(fake_sd) -> None:
    fake_sd.reject = None
    fake_sd.info["broken"] = True
    with pytest.raises(AudioCaptureError, match="consulta del dispositivo"):
        AudioCapture().start()


def test_start_falla_si_el_stream_no_arranca(fake_sd) -> None:
    fake_sd.fail_start = True
    cap = AudioCapture()
    with pytest.raises(AudioCaptureError, match="No se pudo iniciar"):
        cap.start()
    (stream,) = fake_sd.instances
    assert stream.closed and not cap.running


def test_list_input_devices(monkeypatch) -> None:
    if audio_capture.sd is None:
        pytest.skip("PortAudio no disponible")
    devices = [
        {"name": "Altavoces", "index": 0, "hostapi": 0, "max_input_channels": 0,
         "default_samplerate": 48000.0},
        {"name": "Micrófono (PRO X)", "index": 1, "hostapi": 0, "max_input_channels": 1,
         "default_samplerate": 44100.0},
        {"name": "Micrófono (PRO X)", "index": 2, "hostapi": 1, "max_input_channels": 1,
         "default_samplerate": 48000.0},
    ]

    def query_devices(device=None, kind=None):
        if kind == "input":
            return devices[1]
        return devices

    monkeypatch.setattr(audio_capture.sd, "query_devices", query_devices)
    monkeypatch.setattr(audio_capture.sd, "query_hostapis",
                        lambda index=None: ({"name": "MME"}, {"name": "Windows WASAPI"}))
    assert list_input_devices() == [
        {"index": 1, "name": "Micrófono (PRO X)", "hostapi": "MME",
         "default_samplerate": 44100.0, "is_default": True},
        {"index": 2, "name": "Micrófono (PRO X)", "hostapi": "Windows WASAPI",
         "default_samplerate": 48000.0, "is_default": False},
    ]


# ---------------------------------------------------------------- AudioFileSource

def test_fuente_de_archivo_en_tiempo_real_y_silencio_al_final(tmp_path: Path) -> None:
    tone = _sine(440, int(0.3 * SR), SR, amp=0.5)
    path = _write_wav(tmp_path / "tono.wav", tone)
    src = AudioFileSource(path, window_s=1.0, block_s=0.05)
    assert src.sample_rate == SR and src.duration_s is None and not src.running

    t0 = time.monotonic()
    src.start()
    assert src.running and src.last_callback_ts >= t0
    assert src.duration_s == pytest.approx(0.3, abs=0.01)
    _wait_until(lambda: src.total_samples >= 1600)
    assert src.level_db == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.3)
    _wait_until(lambda: src.total_samples >= 8000)  # 0,5 s: pasa el final del archivo
    elapsed = time.monotonic() - t0
    src.stop()
    assert elapsed >= 0.4  # respeta el reloj: 0,5 s de audio no llegan antes
    assert not src.running and src.finished
    assert src.level_db == LEVEL_FLOOR_DB
    src.stop()  # idempotente

    data, total = src.read_window(1.0)
    assert total == src.total_samples and data.size == total
    n = tone.size
    assert np.allclose(data[:n], tone, atol=2e-4)
    assert not data[n:].any()  # después del archivo, silencio


def test_fuente_de_archivo_reanuda_donde_quedo(tmp_path: Path) -> None:
    ramp = (np.arange(SR) / SR * 0.9).astype(np.float32)  # valores distintos en cada muestra
    path = _write_wav(tmp_path / "rampa.wav", ramp)
    src = AudioFileSource(path, window_s=1.0, block_s=0.05, speed=2.0)
    src.start()
    src.start()  # idempotente
    _wait_until(lambda: src.total_samples >= 3200)
    src.stop()
    first_total = src.total_samples
    assert src.position_s * SR == first_total and not src.finished

    src.start()
    _wait_until(lambda: src.total_samples >= first_total + 3200)
    src.stop()
    data, total = src.read_window(1.0, since_sample=first_total)
    assert total == src.total_samples and data.size == total - first_total
    decoded = np.round(ramp * 32767) / 32768  # mismo redondeo que el WAV de 16 bits
    assert np.allclose(data, decoded[first_total:first_total + data.size], atol=1e-4)


def test_fuente_de_archivo_remuestrea_a_16k(tmp_path: Path) -> None:
    path = _write_wav(tmp_path / "tono44k.wav", _sine(440, 44100 // 5, 44100), sr=44100)
    src = AudioFileSource(path, window_s=1.0, block_s=0.05, speed=4.0)
    src.start()
    try:
        assert src.duration_s == pytest.approx(0.2, abs=0.01)
    finally:
        src.stop()


def test_fuente_de_archivo_inexistente(tmp_path: Path) -> None:
    src = AudioFileSource(tmp_path / "no_existe.wav")
    with pytest.raises(AudioCaptureError, match="No se pudo leer"):
        src.start()
    assert not src.running
    src.stop()


def test_fuente_de_archivo_parametros_invalidos(tmp_path: Path) -> None:
    for kwargs in ({"block_s": 0}, {"speed": 0}, {"window_s": 0}):
        with pytest.raises(ValueError):
            AudioFileSource(tmp_path / "x.wav", **kwargs)
