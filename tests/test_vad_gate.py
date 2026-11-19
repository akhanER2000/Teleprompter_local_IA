"""Puerta de voz con Silero VAD (docs/diseno_v2.md, sección 8; Fase 2, paso 2).

Silero corre en CPU con el modelo ONNX que trae faster-whisper: sin GPU ni red.

`tests/fixtures/voz_sintetica_es.wav` es voz SINTÉTICA (no contiene la voz de
ninguna persona): la frase "Esta es una prueba de voz sintética en español."
generada con Windows TTS (System.Speech, voz "Microsoft Sabina Desktop",
`SpeechAudioFormatInfo` de 16 kHz, 16 bits, mono, `SetOutputToWaveFile`) y
recortada a unos 80 ms de silencio a cada lado (2,77 s, 88 KB).
"""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from tools import vad_gate
from tools.vad_gate import VadGate

SR = 16000
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "voz_sintetica_es.wav"
JOIN_TIMEOUT_S = 5.0


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2)
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0


@pytest.fixture(scope="module")
def speech() -> np.ndarray:
    return _load_wav(FIXTURE)


@pytest.fixture(scope="module")
def gate() -> VadGate:
    return VadGate()


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def _at_end(clip: np.ndarray, seconds: float) -> np.ndarray:
    """`clip` al final de una ventana de `seconds` segundos, con silencio antes."""
    return np.concatenate([np.zeros(int(seconds * SR) - clip.size, dtype=np.float32), clip])


def _white_noise(seconds: float, dbfs: float, seed: int = 1234) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rms = 10.0 ** (dbfs / 20.0)
    return (rng.standard_normal(int(seconds * SR)) * rms).astype(np.float32)


def _tone(seconds: float, freq_hz: float = 440.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)


# ---------------------------------------------------------------- fixture

def test_fixture_es_voz_sintetica_corta(speech: np.ndarray) -> None:
    assert FIXTURE.stat().st_size < 120 * 1024
    assert 2.0 <= speech.size / SR <= 3.5
    assert 0.1 < float(np.abs(speech).max()) <= 1.0


# ---------------------------------------------------------------- sin voz

@pytest.mark.parametrize("name,window", [
    ("silencio digital", _silence(3.0)),
    ("ruido blanco -50 dBFS", _white_noise(3.0, -50.0)),
    ("tono 440 Hz", _tone(3.0)),
])
def test_sin_voz(gate: VadGate, name: str, window: np.ndarray) -> None:
    assert gate.has_speech(window) is False, name
    assert gate.has_speech(window, tail_s=3.0) is False, name
    assert gate.speech_ratio(window) == 0.0, name


# ---------------------------------------------------------------- con voz

def test_voz_sintetica(gate: VadGate, speech: np.ndarray) -> None:
    assert gate.has_speech(speech) is True
    assert gate.speech_ratio(speech) > 0.8


def test_voz_al_final_de_una_ventana_de_3_s(gate: VadGate, speech: np.ndarray) -> None:
    window = _at_end(speech, 3.0)
    assert window.size == 3 * SR
    assert gate.has_speech(window, tail_s=1.0) is True
    assert 0.5 < gate.speech_ratio(window) < 1.0


def test_voz_con_ruido_de_fondo(gate: VadGate, speech: np.ndarray) -> None:
    window = _at_end(speech, 3.0)
    assert gate.has_speech(window + _white_noise(3.0, -40.0)) is True


def test_voz_solo_en_el_primer_segundo_no_cuenta(gate: VadGate, speech: np.ndarray) -> None:
    window = np.concatenate([speech[:SR], _silence(2.0)])
    assert gate.has_speech(window, tail_s=1.0) is False
    # La voz sí está en la ventana: con una cola más larga se detecta.
    assert gate.has_speech(window, tail_s=2.5) is True
    assert 0.0 < gate.speech_ratio(window) < 0.5


def test_ventana_mas_larga_que_el_contexto(gate: VadGate, speech: np.ndarray) -> None:
    # Solo se analiza la cola más CONTEXT_S: la voz del principio no cuenta.
    window = np.concatenate([speech, _silence(4.0)])
    assert gate.has_speech(window, tail_s=1.0) is False
    window = np.concatenate([_silence(4.0), speech])
    assert gate.has_speech(window, tail_s=1.0) is True


# ---------------------------------------------------------------- entradas

def test_ventanas_vacias_o_muy_cortas(gate: VadGate, speech: np.ndarray) -> None:
    assert gate.has_speech(np.zeros(0, dtype=np.float32)) is False
    assert gate.has_speech(speech[:511]) is False
    assert gate.speech_ratio(speech[:100]) == 0.0


def test_acepta_otros_tipos_sin_modificar_la_entrada(gate: VadGate, speech: np.ndarray) -> None:
    window = speech.astype(np.float64)
    window[:10] = np.nan
    before = window.copy()
    assert gate.has_speech(window) is True
    np.testing.assert_array_equal(window, before)
    assert gate.has_speech(speech.reshape(1, -1)) is True  # (1, n) se aplana


@pytest.mark.parametrize("kwargs", [
    {"threshold": 0.0}, {"threshold": 1.0}, {"threshold": -0.2}, {"sample_rate": 8000},
])
def test_parametros_invalidos(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        VadGate(**kwargs)


@pytest.mark.parametrize("tail_s", [0.0, -1.0, float("nan"), float("inf")])
def test_tail_invalida(gate: VadGate, tail_s: float) -> None:
    with pytest.raises(ValueError):
        gate.has_speech(_silence(1.0), tail_s=tail_s)


# ---------------------------------------------------------------- carga y coste

def test_carga_unica_entre_hilos(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = vad_gate.get_vad_model

    def slow_load():
        calls.append(1)
        time.sleep(0.05)  # ensancha la carrera entre hilos
        return real()

    monkeypatch.setattr(vad_gate, "_model_ready", False)
    monkeypatch.setattr(vad_gate, "get_vad_model", slow_load)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=JOIN_TIMEOUT_S)
        VadGate().load()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT_S)
    assert not any(t.is_alive() for t in threads)
    assert len(calls) == 1


def test_consultas_concurrentes_coinciden(gate: VadGate, speech: np.ndarray) -> None:
    windows = [speech, _silence(3.0), _tone(3.0)] * 4
    results: list[bool | None] = [None] * len(windows)

    def worker(i: int) -> None:
        results[i] = gate.has_speech(windows[i])

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(len(windows))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT_S)
    assert results == [True, False, False] * 4


def test_es_rapido_en_cpu(gate: VadGate, speech: np.ndarray) -> None:
    # Medido: unos 3 ms por ventana de 3 s. El tope es holgado para no
    # fallar en una máquina cargada; detecta regresiones de orden de magnitud.
    window = _at_end(speech, 3.0)
    gate.has_speech(window)  # carga del modelo fuera de la medición
    times = []
    for _ in range(7):
        t0 = time.perf_counter()
        gate.has_speech(window)
        times.append(time.perf_counter() - t0)
    assert sorted(times)[3] < 0.060
