"""Motor STT sin modelo real (docs/diseno_v2.md, sección 9).

`WhisperModel`, `download_model` y el registro de DLL se sustituyen por
dobles: nada carga Whisper, usa la GPU ni la red.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from tools import stt_engine
from tools.stt_engine import SegmentInfo, STTEngine, STTError, Transcription

EXPECTED_DECODING = {
    "beam_size": 1,
    "best_of": 1,
    "temperature": 0.0,
    "condition_on_previous_text": False,
    "without_timestamps": True,
    "vad_filter": False,
}


def _segment(text: str, no_speech: float = 0.1, logprob: float = -0.3, ratio: float = 1.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob,
                           compression_ratio=ratio)


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (tmp_path / name).write_bytes(b"")
    return tmp_path


@pytest.fixture
def env(monkeypatch, model_dir: Path):
    """Dobles de faster-whisper. `env.fail` decide qué falla en cada dispositivo."""
    state = SimpleNamespace(
        dll_calls=0, downloads=[], models=[], fail={}, download_error=None,
        replies=[], model_dir=model_dir,
    )

    def register_cuda_dlls():
        state.dll_calls += 1
        return []

    def download_model(size, local_files_only=False, **kwargs):
        state.downloads.append((size, local_files_only))
        if state.download_error is not None:
            raise state.download_error
        return str(model_dir)

    class FakeWhisperModel:
        def __init__(self, path, device="auto", compute_type="default",
                     local_files_only=False, **kwargs):
            self.path, self.device, self.compute_type = path, device, compute_type
            self.local_files_only = local_files_only
            self.calls: list[tuple[np.ndarray, dict]] = []
            state.models.append(self)
            error = state.fail.get((device, "init"))
            if error is not None:
                raise error

        def transcribe(self, audio, **kwargs):
            self.calls.append((audio, kwargs))
            error = state.fail.get((self.device, "transcribe"))
            if error is not None:
                raise error
            segments = state.replies.pop(0) if state.replies else []
            return iter(segments), SimpleNamespace(language=kwargs.get("language"))

    monkeypatch.setattr(stt_engine, "register_cuda_dlls", register_cuda_dlls)
    monkeypatch.setattr(stt_engine, "download_model", download_model)
    monkeypatch.setattr(stt_engine, "WhisperModel", FakeWhisperModel)
    return state


# ---------------------------------------------------------------- carga

def test_carga_en_cuda_con_warmup(env) -> None:
    eng = STTEngine("medium", "cuda", "float16", "es")
    assert not eng.loaded and eng.device_in_use == "cuda"
    eng.load()

    assert env.dll_calls == 1
    assert env.downloads == [("medium", True)]
    (model,) = env.models
    assert model.path == str(env.model_dir)
    assert (model.device, model.compute_type, model.local_files_only) == ("cuda", "float16", True)
    assert eng.loaded and eng.model is model
    assert (eng.device_in_use, eng.compute_type_in_use, eng.fallback_reason) == ("cuda", "float16", None)
    assert eng.model_path == str(env.model_dir)

    # Warmup: 1 s de silencio con los mismos parámetros de decodificación.
    ((audio, kwargs),) = model.calls
    assert audio.dtype == np.float32 and audio.shape == (16000,) and not audio.any()
    assert kwargs["language"] == "es" and kwargs["initial_prompt"] is None
    assert {k: kwargs[k] for k in EXPECTED_DECODING} == EXPECTED_DECODING


def test_model_missing_si_no_esta_en_la_cache(env) -> None:
    env.download_error = LocalEntryNotFoundError("Cannot find an appropriate cached snapshot")
    eng = STTEngine("medium", "cuda", "float16", "es")
    with pytest.raises(STTError) as info:
        eng.load()
    assert info.value.code == "MODEL_MISSING"
    assert "install.bat" in info.value.message and "'medium'" in info.value.message
    assert str(info.value) == info.value.message
    assert env.models == [] and not eng.loaded  # sin fallback a CPU


def test_model_missing_desde_whisper_model_no_reintenta_en_cpu(env) -> None:
    env.fail[("cuda", "init")] = LocalEntryNotFoundError("no cache")
    eng = STTEngine("medium", "cuda", "float16", "es")
    with pytest.raises(STTError) as info:
        eng.load()
    assert info.value.code == "MODEL_MISSING"
    assert [m.device for m in env.models] == ["cuda"]
    assert eng.fallback_reason is None


@pytest.mark.parametrize("missing", ["model.bin", "tokenizer.json", "config.json", "vocabulary.txt"])
def test_model_missing_si_la_carpeta_esta_incompleta(env, missing: str) -> None:
    (env.model_dir / missing).unlink()
    with pytest.raises(STTError) as info:
        STTEngine("medium", "cuda", "float16", "es").load()
    assert info.value.code == "MODEL_MISSING"
    assert missing.split(".")[0] in info.value.message
    assert env.models == []


def test_model_missing_con_la_cache_real_sin_red(monkeypatch) -> None:
    """La excepción real de huggingface_hub 2.2 con local_files_only=True."""
    monkeypatch.setattr(stt_engine, "register_cuda_dlls", lambda: [])
    eng = STTEngine("Systran/faster-whisper-no-existe-tp", "cpu", "int8", "es")
    with pytest.raises(STTError) as info:
        eng.load()
    assert info.value.code == "MODEL_MISSING"
    assert isinstance(info.value.__cause__, LocalEntryNotFoundError)


def test_nombre_de_modelo_invalido(env) -> None:
    env.download_error = ValueError("Invalid model size 'gigante'")
    with pytest.raises(STTError) as info:
        STTEngine("gigante", "cuda", "float16", "es").load()
    assert info.value.code == "MODEL_ERROR"


def test_ruta_local_no_usa_la_cache(env) -> None:
    eng = STTEngine(str(env.model_dir), "cpu", "int8", "es")
    eng.load()
    assert env.downloads == []
    assert env.models[0].path == str(env.model_dir)


def test_fallback_a_cpu_si_cuda_falla_al_cargar(env) -> None:
    env.fail[("cuda", "init")] = RuntimeError("CUDA failed with error no CUDA-capable device")
    eng = STTEngine("medium", "cuda", "float16", "es")
    eng.load()
    assert [(m.device, m.compute_type) for m in env.models] == [("cuda", "float16"), ("cpu", "int8")]
    assert (eng.device_in_use, eng.compute_type_in_use) == ("cpu", "int8")
    assert eng.model is env.models[1]
    assert "RuntimeError" in eng.fallback_reason and "CUDA failed" in eng.fallback_reason


def test_fallback_a_cpu_si_falla_el_warmup(env) -> None:
    env.fail[("cuda", "transcribe")] = RuntimeError("Library cublas64_12.dll is not found")
    eng = STTEngine("medium", "cuda", "float16", "es")
    eng.load()
    assert eng.device_in_use == "cpu" and eng.model is env.models[1]
    assert "cublas64_12.dll" in eng.fallback_reason
    assert len(env.models[1].calls) == 1  # warmup en CPU


def test_error_si_falla_tambien_en_cpu(env) -> None:
    env.fail[("cuda", "init")] = RuntimeError("sin CUDA")
    env.fail[("cpu", "init")] = RuntimeError("model.bin corrupto")
    eng = STTEngine("medium", "cuda", "float16", "es")
    with pytest.raises(STTError) as info:
        eng.load()
    assert info.value.code == "MODEL_ERROR"
    assert "sin CUDA" in info.value.message and "model.bin corrupto" in info.value.message
    assert not eng.loaded


def test_en_cpu_no_hay_fallback(env) -> None:
    env.fail[("cpu", "init")] = RuntimeError("roto")
    with pytest.raises(STTError) as info:
        STTEngine("medium", "cpu", "int8", "es").load()
    assert info.value.code == "MODEL_ERROR"
    assert len(env.models) == 1


CPU_TYPES = frozenset({"int8", "int8_float32", "float32", "int16"})  # CTranslate2 4.7 en x86


@pytest.mark.parametrize("compute", ["float16", "int8_float16", "bfloat16", "int8_bfloat16"])
def test_cpu_con_tipo_no_admitido_usa_int8(env, monkeypatch, compute: str) -> None:
    # TP_DEVICE=cpu con el TP_COMPUTE por defecto (float16) daba MODEL_ERROR.
    monkeypatch.setattr(stt_engine, "cpu_compute_types", lambda: CPU_TYPES)
    eng = STTEngine("medium", "cpu", compute, "es")
    eng.load()
    assert [(m.device, m.compute_type) for m in env.models] == [("cpu", "int8")]
    assert (eng.device_in_use, eng.compute_type_in_use) == ("cpu", "int8")
    assert eng.fallback_reason is None  # no es un fallback de CUDA


@pytest.mark.parametrize("compute", ["int8", "int8_float32", "float32", "int16", "auto", "default"])
def test_cpu_con_tipo_admitido_lo_respeta(env, monkeypatch, compute: str) -> None:
    monkeypatch.setattr(stt_engine, "cpu_compute_types", lambda: CPU_TYPES)
    eng = STTEngine("medium", "cpu", compute, "es")
    eng.load()
    assert [(m.device, m.compute_type) for m in env.models] == [("cpu", compute)]
    assert eng.compute_type_in_use == compute


def test_cuda_no_consulta_los_tipos_de_la_cpu(env, monkeypatch) -> None:
    def boom():
        raise AssertionError("no debía consultarse")

    monkeypatch.setattr(stt_engine, "cpu_compute_types", boom)
    STTEngine("medium", "cuda", "float16", "es").load()
    assert [(m.device, m.compute_type) for m in env.models] == [("cuda", "float16")]


def test_cpu_compute_types_reales() -> None:
    types = stt_engine.cpu_compute_types()
    assert isinstance(types, frozenset)
    assert {"int8", "float32"} <= types


# ---------------------------------------------------------------- transcripción

def test_transcribe_parametros_y_resultado(env) -> None:
    eng = STTEngine("medium", "cuda", "float16", "es")
    eng.load()
    env.replies.append([_segment(" Hola, mundo."), _segment(" Esto es la RTX 5070.", 0.2, -0.5, 1.4)])

    window = np.linspace(-0.5, 0.5, 32000)  # float64: se convierte
    result = eng.transcribe(window, initial_prompt="Texto previo del guion")

    assert isinstance(result, Transcription)
    assert result.text == "Hola, mundo. Esto es la RTX 5070."
    assert result.segments == [
        SegmentInfo("Hola, mundo.", 0.1, -0.3, 1.2),
        SegmentInfo("Esto es la RTX 5070.", 0.2, -0.5, 1.4),
    ]
    assert isinstance(result.stt_ms, int) and result.stt_ms >= 0

    audio, kwargs = env.models[0].calls[-1]
    assert audio.dtype == np.float32 and audio.ndim == 1 and audio.flags.c_contiguous
    assert np.allclose(audio, window)
    assert kwargs["language"] == "es"
    assert kwargs["initial_prompt"] == "Texto previo del guion"
    assert {k: kwargs[k] for k in EXPECTED_DECODING} == EXPECTED_DECODING


@pytest.mark.parametrize("prompt", [None, ""])
def test_transcribe_sin_prompt(env, prompt) -> None:
    eng = STTEngine("medium", "cuda", "float16", "en")
    eng.load()
    assert eng.transcribe(np.zeros((8000, 1), np.float32), initial_prompt=prompt).text == ""
    audio, kwargs = env.models[0].calls[-1]
    assert audio.shape == (8000,)
    assert kwargs["initial_prompt"] is None and kwargs["language"] == "en"


def test_transcribe_vacio_no_llama_al_modelo(env) -> None:
    eng = STTEngine("medium", "cuda", "float16", "es")
    eng.load()
    calls = len(env.models[0].calls)
    assert eng.transcribe(np.zeros(0, np.float32)) == Transcription("", [], 0)
    assert len(env.models[0].calls) == calls


def test_transcribe_antes_de_cargar() -> None:
    with pytest.raises(STTError) as info:
        STTEngine().transcribe(np.zeros(16000, np.float32))
    assert info.value.code == "MODEL_NOT_READY"


def test_transcribe_error_del_modelo(env) -> None:
    eng = STTEngine("medium", "cpu", "int8", "es")
    eng.load()
    env.fail[("cpu", "transcribe")] = RuntimeError("fallo interno")
    with pytest.raises(STTError) as info:
        eng.transcribe(np.zeros(16000, np.float32))
    assert info.value.code == "MODEL_ERROR"
    assert isinstance(info.value.__cause__, RuntimeError)
