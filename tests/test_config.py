"""Configuración central (backend/config.py): precedencia, validación y rutas."""
from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from backend import config
from backend.config import HOST, Settings, load_settings


@pytest.fixture
def no_file(tmp_path: Path) -> Path:
    return tmp_path / "no_existe.json"


def _write(tmp_path: Path, data, name: str = "config.json") -> Path:
    path = tmp_path / name
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_sin_entorno_ni_archivo(no_file: Path) -> None:
    s = load_settings(env={}, config_path=no_file)
    assert s == Settings()
    assert (s.port, s.model_size, s.device, s.compute_type, s.language) == (
        8765, "medium", "cuda", "float16", "es")
    assert s.input_device is None and s.audio_file is None
    assert (s.window_s, s.step_ms, s.vad_threshold) == (3.0, 350, 0.5)
    assert not (s.prompt_context or s.debug or s.fake_stt)
    assert s.log_level == "INFO"


def test_host_siempre_local(tmp_path: Path) -> None:
    path = _write(tmp_path, {"host": "0.0.0.0"})
    s = load_settings(env={"TP_HOST": "0.0.0.0"}, config_path=path)
    assert s.host == HOST == "127.0.0.1"
    assert "host" not in {f.name for f in dataclasses.fields(Settings)}


def test_settings_inmutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().port = 1  # type: ignore[misc]


def test_precedencia_entorno_archivo_defecto(tmp_path: Path) -> None:
    path = _write(tmp_path, {"model_size": "small", "step_ms": 400, "debug": True})
    s = load_settings(env={"TP_MODEL_SIZE": "large-v3-turbo"}, config_path=path)
    assert s.model_size == "large-v3-turbo"   # entorno
    assert s.step_ms == 400                   # config.json
    assert s.debug is True                    # config.json
    assert s.port == 8765                     # defecto


def test_archivo_acepta_nombres_de_variable(tmp_path: Path) -> None:
    path = _write(tmp_path, {"TP_PORT": 9001, "TP_COMPUTE": "int8", "TP_INPUT_DEVICE": 3})
    s = load_settings(env={}, config_path=path)
    assert (s.port, s.compute_type, s.input_device) == (9001, "int8", 3)


def test_variable_vacia_cuenta_como_no_definida(tmp_path: Path) -> None:
    path = _write(tmp_path, {"language": "en"})
    s = load_settings(env={"TP_LANGUAGE": "  ", "TP_INPUT_DEVICE": ""}, config_path=path)
    assert s.language == "en"
    assert s.input_device is None


@pytest.mark.parametrize("var,raw,field,expected", [
    ("TP_PORT", "8080", "port", 8080),
    ("TP_DEVICE", "CPU", "device", "cpu"),
    ("TP_COMPUTE", "int8_float16", "compute_type", "int8_float16"),
    ("TP_LANGUAGE", "EN", "language", "en"),
    ("TP_INPUT_DEVICE", "3", "input_device", 3),
    ("TP_WINDOW_S", "2,5", "window_s", 2.5),
    ("TP_STEP_MS", " 500 ", "step_ms", 500),
    ("TP_PROMPT_CONTEXT", "1", "prompt_context", True),
    ("TP_VAD_THRESHOLD", "0.35", "vad_threshold", 0.35),
    ("TP_DEBUG", "true", "debug", True),
    ("TP_DEBUG", "on", "debug", True),
    ("TP_FAKE_STT", "0", "fake_stt", False),
    ("TP_FAKE_STT", "yes", "fake_stt", True),
    ("TP_LOG_LEVEL", "debug", "log_level", "DEBUG"),
])
def test_valores_validos_del_entorno(no_file: Path, var: str, raw: str, field: str,
                                    expected) -> None:
    s = load_settings(env={var: raw}, config_path=no_file)
    assert getattr(s, field) == expected


@pytest.mark.parametrize("var,raw", [
    ("TP_PORT", "abc"), ("TP_PORT", "0"), ("TP_PORT", "70000"), ("TP_PORT", "80.5"),
    ("TP_DEVICE", "tpu"), ("TP_COMPUTE", "float8"), ("TP_LANGUAGE", "español"),
    ("TP_INPUT_DEVICE", "-1"), ("TP_INPUT_DEVICE", "mic"),
    ("TP_WINDOW_S", "0.2"), ("TP_WINDOW_S", "nan"), ("TP_WINDOW_S", "inf"),
    ("TP_STEP_MS", "5"), ("TP_STEP_MS", "1e3"),
    ("TP_PROMPT_CONTEXT", "quizas"), ("TP_VAD_THRESHOLD", "0"), ("TP_VAD_THRESHOLD", "1.5"),
    ("TP_DEBUG", "2"), ("TP_LOG_LEVEL", "VERBOSE"),
    ("TP_AUDIO_FILE", "no/existe/nada.wav"),
])
def test_valor_invalido_usa_defecto_y_avisa(no_file: Path, caplog: pytest.LogCaptureFixture,
                                            var: str, raw: str) -> None:
    field = config._FIELD_BY_VAR[var]
    with caplog.at_level(logging.WARNING, logger="teleprompter.config"):
        s = load_settings(env={var: raw}, config_path=no_file)
    assert getattr(s, field) == getattr(Settings(), field)
    assert any(var in r.getMessage() for r in caplog.records)


def test_invalido_en_entorno_no_cae_al_archivo(tmp_path: Path) -> None:
    # Contrato: un valor inválido se reemplaza por el defecto, no por la fuente siguiente.
    path = _write(tmp_path, {"port": 9000})
    assert load_settings(env={"TP_PORT": "x"}, config_path=path).port == 8765


@pytest.mark.parametrize("data", [
    {"port": True},            # bool no es entero
    {"port": "9000x"},
    {"window_s": [3]},
    {"debug": "tal vez"},
    {"input_device": -2},
    {"model_size": ""},
    {"model_size": 5},
])
def test_valores_json_invalidos(tmp_path: Path, data: dict) -> None:
    path = _write(tmp_path, data)
    assert load_settings(env={}, config_path=path) == Settings()


def test_valores_json_tipados(tmp_path: Path) -> None:
    path = _write(tmp_path, {"window_s": 4, "vad_threshold": 0.6, "prompt_context": 1,
                             "input_device": None, "fake_stt": False})
    s = load_settings(env={}, config_path=path)
    assert (s.window_s, s.vad_threshold, s.prompt_context, s.input_device, s.fake_stt) == (
        4.0, 0.6, True, None, False)


@pytest.mark.parametrize("content", ["{no es json", "[1, 2, 3]", "\"texto\"", ""])
def test_archivo_invalido_se_ignora(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                    content: str) -> None:
    path = _write(tmp_path, content)
    with caplog.at_level(logging.WARNING, logger="teleprompter.config"):
        s = load_settings(env={"TP_STEP_MS": "300"}, config_path=path)
    assert s == Settings(step_ms=300)
    assert caplog.records


def test_archivo_con_bom_y_clave_desconocida(tmp_path: Path,
                                             caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"debug": 1, "volumen": 11}), encoding="utf-8-sig")
    with caplog.at_level(logging.WARNING, logger="teleprompter.config"):
        s = load_settings(env={}, config_path=path)
    assert s.debug is True
    assert any("volumen" in r.getMessage() for r in caplog.records)


def test_audio_file_existente_se_resuelve(tmp_path: Path) -> None:
    wav = tmp_path / "voz.wav"
    wav.write_bytes(b"RIFF")
    s = load_settings(env={"TP_AUDIO_FILE": str(wav)}, config_path=tmp_path / "x.json")
    assert s.audio_file is not None
    assert Path(s.audio_file) == wav.resolve()


def test_audio_file_relativo_a_la_raiz(no_file: Path) -> None:
    s = load_settings(env={"TP_AUDIO_FILE": "guiones/ejemplo.txt"}, config_path=no_file)
    assert s.audio_file is not None
    assert Path(s.audio_file) == (config.ROOT / "guiones" / "ejemplo.txt").resolve()


def test_por_defecto_lee_el_entorno_del_proceso(monkeypatch: pytest.MonkeyPatch,
                                                no_file: Path) -> None:
    monkeypatch.setenv("TP_STEP_MS", "420")
    assert load_settings(config_path=no_file).step_ms == 420


def test_rutas_del_proyecto() -> None:
    assert config.ROOT == Path(__file__).resolve().parent.parent
    assert config.FRONTEND_DIR == config.ROOT / "frontend"
    assert config.SCRIPTS_DIR == config.ROOT / "guiones"
    assert config.DATA_DIR == config.ROOT / "data"
    assert config.LOG_DIR == config.ROOT / "logs"
    assert (config.FRONTEND_DIR / "index.html").is_file()
