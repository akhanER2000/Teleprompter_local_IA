"""Replay y muestras sintéticas (Fase 2, punto 8): métricas, reloj virtual y tiempos del TTS.

Nada usa GPU, micrófono, red ni el TTS de Windows: las líneas de tiempo y
referencias se arman a mano, el STT y el VAD son falsos y el reloj del
replay es inyectable.
"""
from __future__ import annotations

import csv
import itertools
from types import SimpleNamespace

import numpy as np
import pytest

from tools import replay, synth_sample
from tools.replay import (
    RefWord,
    ReplayConfig,
    TimelineRow,
    compute_metrics,
    expand_sweep,
    prefer_first_occurrence,
    reference_from_words,
    simulate,
    verdict,
)
from tools.script_parser import parse_script
from tools.synth_sample import Plan, Span, build_plan, map_progress_to_words, render_text

SR = 16000


def _ref(*items: tuple[int, float, float]) -> list[RefWord]:
    return [RefWord(tok, f"t{tok}", start, end) for tok, start, end in items]


def _row(t_emit: float, before: int, after: int, **kw) -> TimelineRow:
    kw.setdefault("t_audio_ms", t_emit - 100)
    kw.setdefault("voiced", True)
    kw.setdefault("advanced", after > before)
    return TimelineRow(t_emit_ms=t_emit, idx_before=before, idx_after=after, **kw)


def _steady_ref(n: int, word_ms: float = 300.0) -> list[RefWord]:
    return _ref(*[(k, k * word_ms, (k + 1) * word_ms) for k in range(n)])


# ---------------------------------------------------------------- métricas

def test_metricas_retraso_cobertura_y_nunca_pasados() -> None:
    ref = _steady_ref(4)  # tokens 0..3; el guion tiene 6
    timeline = [
        _row(600, 0, 1),     # pasa el token 0 (fin 300): 300 ms
        _row(1100, 1, 3),    # pasa 1 (fin 600) y 2 (fin 900): 500 y 200 ms
        _row(1600, 3, 3),    # el token 3 nunca se pasa
    ]
    m = compute_metrics(timeline, ref, total_tokens=6)
    assert m["spoken_tokens"] == 4
    assert m["passed_tokens"] == 3
    assert m["never_passed"] == 1
    assert m["coverage"] == pytest.approx(0.75)
    assert m["delay_p50_ms"] == pytest.approx(300)
    assert m["delay_p95_ms"] == pytest.approx(480)  # percentil lineal de [200, 300, 500]
    assert m["delay_max_ms"] == pytest.approx(500)
    assert m["false_jumps"] == 0 and m["regressions"] == 0
    assert m["advances_total"] == 2 and m["emissions"] == 2
    assert m["final_idx"] == 3 and m["cycles"] == 3


def test_metricas_primer_paso_cuenta_aunque_luego_retroceda() -> None:
    ref = _steady_ref(3)
    timeline = [_row(700, 0, 3), _row(900, 3, 1, resync=True), _row(1500, 1, 3)]
    m = compute_metrics(timeline, ref, total_tokens=3)
    assert m["passed_tokens"] == 3
    assert m["delay_max_ms"] == pytest.approx(400)  # token 0: 700 - 300
    assert m["regressions"] == 1 and m["regressions_resync"] == 1


def test_metricas_saltos_falsos_con_umbral_de_cinco() -> None:
    ref = _steady_ref(10)  # el token k empieza en 300k
    timeline = [
        _row(650, 0, 8),   # posición de referencia 3 (token 2 ya empezó): adelanto 5, vale
        _row(700, 8, 9),   # adelanto 6: salto falso
        _row(3000, 9, 10),
    ]
    m = compute_metrics(timeline, ref, total_tokens=10)
    assert m["false_jumps"] == 1
    assert m["max_overshoot"] == 6
    assert "saltos falsos" in verdict(m)


def test_metricas_retrocesos_separa_los_de_resync() -> None:
    ref = _steady_ref(8)
    timeline = [
        _row(2000, 0, 5),
        _row(2300, 5, 3),                 # retroceso sin resync: no debería pasar nunca
        _row(2600, 3, 6),
        _row(2900, 6, 2, resync=True),    # retroceso por resincronización
    ]
    m = compute_metrics(timeline, ref, total_tokens=8)
    assert m["regressions"] == 2
    assert m["regressions_resync"] == 1
    assert "retrocesos" in verdict(m)
    only_resync = compute_metrics([timeline[0], _row(2900, 5, 2, resync=True)], ref,
                                  total_tokens=8)
    assert only_resync["regressions"] == 1 and only_resync["regressions_resync"] == 1
    assert "retrocesos" not in verdict(only_resync)


def test_metricas_tiempo_de_resincronizacion_tras_saltar_un_parrafo() -> None:
    # Se dicen los tokens 0..3 y, tras saltarse el párrafo 4..19, los 20..23.
    ref = _ref((0, 0, 300), (1, 300, 600), (2, 600, 900), (3, 900, 1200),
               (20, 1500, 1800), (21, 1800, 2100), (22, 2100, 2400), (23, 2400, 2700))
    timeline = [
        _row(1300, 0, 4),
        _row(2000, 4, 4, desync=True),
        _row(3700, 4, 21, resync=True, advanced=True),
        _row(4000, 21, 24),
    ]
    m = compute_metrics(timeline, ref, total_tokens=30)
    assert m["skips"] == 1
    assert m["resync_ms"] == [pytest.approx(2200)]   # 3700 - inicio del token 20
    assert m["resync_max_ms"] == pytest.approx(2200)
    assert m["resync_missed"] == 0
    assert m["false_jumps"] == 0 and m["regressions"] == 0
    assert m["desync_cycles"] == 1 and m["resyncs"] == 1
    assert m["coverage"] == pytest.approx(1.0)
    assert not [f for f in verdict(m) if f.startswith("resync")]

    sin_resync = compute_metrics(timeline[:2], ref, total_tokens=30)
    assert sin_resync["resync_ms"] == [None] and sin_resync["resync_missed"] == 1
    assert any(f.startswith("resync") for f in verdict(sin_resync))


def test_metricas_relectura_usa_la_primera_vez_y_no_es_un_salto() -> None:
    ref = _ref((0, 0, 300), (1, 300, 600), (2, 600, 900), (3, 900, 1200),
               (1, 1300, 1600), (2, 1600, 1900), (3, 1900, 2200), (4, 2300, 2600))
    timeline = [_row(1400, 0, 4), _row(2900, 4, 5)]
    m = compute_metrics(timeline, ref, total_tokens=5)
    assert m["spoken_tokens"] == 5 and m["skips"] == 0
    assert m["delay_max_ms"] == pytest.approx(1400 - 300)  # token 1, primera lectura
    assert m["false_jumps"] == 0


def test_metricas_silencio() -> None:
    quieto = [_row(t, 0, 0, voiced=False) for t in (350, 700, 1050)]
    m = compute_metrics(quieto, {"words": []}, total_tokens=89)
    assert m["advances_total"] == 0 and m["spoken_tokens"] == 0
    assert m["coverage"] is None and m["delay_p50_ms"] is None
    assert m["stt_ms_mean"] is None and m["voiced_cycles"] == 0
    assert verdict(m) == []

    con_avance = compute_metrics([*quieto, _row(1400, 0, 2)], None, total_tokens=89)
    assert con_avance["advances_total"] == 1
    assert con_avance["false_jumps"] == 0  # 2 tokens no superan el umbral de 5
    assert verdict(con_avance) == ["avances en silencio"]


def test_metricas_aceptan_dicts_y_estadisticas_de_stt() -> None:
    ref = {"words": [{"token_idx": 0, "text": "Hola", "start_ms": 0, "end_ms": 400}]}
    timeline = [
        {"t_audio_ms": 1000, "t_emit_ms": 1150, "voiced": True, "idx_before": 0,
         "idx_after": 1, "advanced": True, "stt_ms": 120},
        {"t_audio_ms": 1350, "t_emit_ms": 1530, "voiced": True, "dropped": "blacklist",
         "idx_before": 1, "idx_after": 1, "stt_ms": 180},
    ]
    m = compute_metrics(timeline, ref, total_tokens=1)
    assert m["delay_p50_ms"] == pytest.approx(750)
    assert m["stt_ms_mean"] == pytest.approx(150) and m["stt_ms_max"] == 180
    assert m["proc_ms_mean"] == pytest.approx(165)
    assert m["dropped_cycles"] == 1


def test_metricas_ignoran_tokens_fuera_del_guion() -> None:
    ref = _ref((0, 0, 300), (7, 300, 600))
    m = compute_metrics([_row(800, 0, 1)], ref, total_tokens=3)
    assert m["spoken_tokens"] == 1 and m["coverage"] == pytest.approx(1.0)


# ---------------------------------------------------------------- reloj virtual

class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeMatcher:
    def __init__(self) -> None:
        self.current_idx = 0
        self.prompts = 0

    def context_before(self, n_words: int = 25) -> str:
        self.prompts += 1
        return f"previo {n_words}"

    def text_near(self, before: int = 10, after: int = 50) -> str:
        return "cerca"


class _FakeCycle:
    """Tarda `voiced_s` segundos de reloj cuando la ventana dura 1 s o más."""

    def __init__(self, clock: _Clock, voiced_s: float) -> None:
        self.clock = clock
        self.voiced_s = voiced_s
        self.calls: list[tuple[int, str | None, str]] = []

    def transcribe(self, window: np.ndarray, *, prompt: str | None, near_text: str):
        self.calls.append((window.size, prompt, near_text))
        voiced = window.size >= SR
        if voiced:
            self.clock.now += self.voiced_s
        return SimpleNamespace(voiced=voiced, text="hola" if voiced else "", words=["hola"],
                               stt_ms=int(self.voiced_s * 1000) if voiced else 0, dropped=None)

    def apply(self, out, matcher):
        if not out.voiced:
            return None
        matcher.current_idx += 1
        return SimpleNamespace(new_idx=matcher.current_idx, advanced=True, resync=False,
                               desync=False)


def test_reloj_virtual_salta_ciclos_sin_encolarlos() -> None:
    clock = _Clock()
    cycle = _FakeCycle(clock, voiced_s=0.8)
    matcher = _FakeMatcher()
    rows = simulate(np.zeros(3 * SR, dtype=np.float32), cycle=cycle, matcher=matcher,
                    step_ms=350, window_s=3.0, block_ms=100, timer=clock)
    # 350 y 700: menos de 1 s de audio (sin STT); 1050: STT de 800 ms; el ciclo de
    # 1400 no cabe y no se encola: el siguiente empieza al terminar (1850).
    assert [r.t_audio_ms for r in rows] == pytest.approx([350, 700, 1050, 1850, 2650])
    assert [r.t_emit_ms for r in rows] == pytest.approx([350, 700, 1850, 2650, 3450])
    assert [r.voiced for r in rows] == [False, False, True, True, True]
    # Ventana hasta el último bloque de 100 ms disponible: 300, 700, 1000, 1800, 2600 ms.
    assert [c[0] for c in cycle.calls] == [4800, 11200, 16000, 28800, 41600]
    assert [(r.idx_before, r.idx_after) for r in rows][-1] == (2, 3)
    assert all(r.advanced for r in rows[2:]) and rows[2].stt_ms == 800
    assert matcher.prompts == 0 and all(c[1] is None and c[2] == "cerca" for c in cycle.calls)


def test_reloj_virtual_inferencia_rapida_mantiene_el_paso() -> None:
    clock = _Clock()
    cycle = _FakeCycle(clock, voiced_s=0.1)
    matcher = _FakeMatcher()
    rows = simulate(np.zeros(int(2.5 * SR), dtype=np.float32), cycle=cycle, matcher=matcher,
                    step_ms=350, window_s=1.0, block_ms=0, prompt_context=True,
                    prompt_words=20, timer=clock)
    assert [r.t_audio_ms for r in rows] == pytest.approx([350, 700, 1050, 1400, 1750, 2100, 2450])
    # Sin bloques la ventana termina en t; con window_s=1 nunca supera 1 s.
    assert [c[0] for c in cycle.calls] == [5600, 11200, 16000, 16000, 16000, 16000, 16000]
    assert matcher.prompts == len(rows) and cycle.calls[0][1] == "previo 20"


def test_reloj_virtual_paso_invalido() -> None:
    with pytest.raises(ValueError):
        simulate(np.zeros(SR, dtype=np.float32), cycle=None, matcher=None, step_ms=0,
                 window_s=3.0)


def test_run_replay_usa_el_ciclo_real_con_stt_y_vad_falsos() -> None:
    from backend.fakes import FakeSTTEngine, FakeVadGate

    parsed = parse_script("Buenas tardes a todos. Hoy vamos a presentar el proyecto.")
    stt = FakeSTTEngine()
    stt.load()
    stt.push("Buenas tardes a todos", "Buenas tardes a todos hoy vamos a presentar",
             "hoy vamos a presentar el proyecto")
    counter = itertools.count(0.0, 0.05)  # cada inferencia "tarda" 50 ms
    config = ReplayConfig(step_ms=350, window_s=3.0, pad_s=0.0)
    ref = _ref((0, 0, 300), (1, 300, 600), (2, 600, 700), (3, 700, 1000),
               (4, 1200, 1400), (5, 1400, 1700), (6, 1700, 1800), (7, 1800, 2300),
               (8, 2300, 2400), (9, 2400, 2900))
    result = replay.run_replay(np.zeros(3 * SR, dtype=np.float32), parsed, config=config,
                               ref=ref, stt=stt, vad=FakeVadGate(True),
                               timer=lambda: next(counter))
    rows = result.timeline
    assert [r.voiced for r in rows[:3]] == [False, False, True]  # el primer segundo no llega
    assert rows[2].t_emit_ms == pytest.approx(rows[2].t_audio_ms + 50)
    assert [r.idx_after for r in rows if r.voiced][:3] == [4, 8, 10]
    m = result.metrics
    assert m["final_idx"] == 10 and m["coverage"] == pytest.approx(1.0)
    assert m["false_jumps"] == 0 and m["regressions"] == 0
    assert result.total_tokens == 10 and stt.calls == m["voiced_cycles"]
    assert replay.format_summary(result).startswith("WAV: <muestras>")
    table = replay.format_table([result])
    assert "step=350" in table and "presentar" not in table  # sin texto transcrito


# ---------------------------------------------------------------- referencia offline

def test_prefer_first_occurrence_mueve_la_relectura() -> None:
    words = ["c", "x", "y", "z", "x", "y", "z", "d"]
    pairs = [(0, 0), (4, 1), (5, 2), (6, 3), (7, 4)]
    assert prefer_first_occurrence(words, pairs) == [(0, 0), (1, 1), (2, 2), (3, 3), (7, 4)]
    # Un hueco que no repite lo alineado (una muletilla) no mueve nada.
    words = ["c", "eh", "x", "y"]
    assert prefer_first_occurrence(words, [(0, 0), (2, 1), (3, 2)]) == [(0, 0), (2, 1), (3, 2)]


def test_reference_from_words_reparte_numeros_y_toma_la_primera_lectura() -> None:
    parsed = parse_script("Buenas tardes a todos. Hoy vamos a presentar la RTX cinco mil "
                          "setenta ti y nada más.")
    words = [("Buenas", 0.0, 0.4), ("tardes", 0.4, 0.8), ("a", 0.8, 0.9), ("todos.", 0.9, 1.3),
             ("Hoy", 1.6, 1.8), ("vamos", 1.8, 2.1), ("a", 2.1, 2.2), ("presentar", 2.2, 2.7),
             ("hoy", 3.0, 3.2), ("vamos", 3.2, 3.5), ("a", 3.5, 3.6), ("presentar", 3.6, 4.1),
             ("la", 4.1, 4.2), ("RTX", 4.2, 4.6), ("5070", 4.6, 6.1), ("Ti", 6.1, 6.4),
             ("y", 6.4, 6.5), ("nada", 6.5, 6.8), ("más.", 6.8, 7.2)]
    ref = {w.token_idx: w for w in reference_from_words(words, parsed.tokens)}
    assert sorted(ref) == list(range(parsed.total_tokens))
    hoy = parsed.tokens[4]
    assert hoy.text == "Hoy" and ref[4].start_ms == pytest.approx(1600)  # primera lectura
    assert ref[7].end_ms == pytest.approx(2700)
    # "5070" (1,5 s) se reparte por largo: cinco 5, mil 3, setenta 7 letras.
    assert ref[10].start_ms == pytest.approx(4600) and ref[10].end_ms == pytest.approx(5100)
    assert ref[11].end_ms == pytest.approx(5400)
    assert ref[12].end_ms == pytest.approx(6100)


def test_reference_from_words_interpola_una_palabra_perdida_y_descarta_ruido() -> None:
    parsed = parse_script("La arquitectura general del sistema principal funciona bien.")
    words = [("La", 0.0, 0.2), ("arquitectura", 0.2, 0.9), ("generar", 0.9, 1.3),
             ("del", 1.3, 1.5), ("sistema", 1.5, 2.0), ("xyzzy", 2.0, 2.6),
             ("funciona", 3.0, 3.5), ("bien.", 3.5, 3.9)]
    ref = {w.token_idx: w for w in reference_from_words(words, parsed.tokens)}
    assert 2 in ref  # "generar" coincide de forma difusa con "general"
    assert ref[5].start_ms == pytest.approx(2000) and ref[5].end_ms == pytest.approx(2500)
    assert reference_from_words([("gracias", 0.0, 0.5)], parsed.tokens) == []
    assert reference_from_words([], parsed.tokens) == []


# ---------------------------------------------------------------- configuración y salida

def test_expand_sweep_y_etiquetas() -> None:
    base = ReplayConfig()
    configs = expand_sweep(base, ["step_ms=300,400", "prompt_context=0,1", "min_score=3.5"])
    assert len(configs) == 4
    assert {(c.step_ms, c.prompt_context) for c in configs} == {
        (300, False), (300, True), (400, False), (400, True)}
    assert all(c.matcher_params == {"min_score": 3.5} for c in configs)
    assert base.matcher_params == {}
    assert configs[0].label() == "step=300 win=3 prompt=0 medium min_score=3.5"
    with pytest.raises(ValueError):
        expand_sweep(base, ["step_ms"])
    with pytest.raises(ValueError):
        expand_sweep(base, ["prompt_context=quizas"])


def test_config_a_settings() -> None:
    cfg = ReplayConfig(step_ms=300, window_s=2.5, prompt_context=True, model="large-v3-turbo")
    s = cfg.to_settings()
    assert (s.step_ms, s.window_s, s.prompt_context, s.model_size) == (300, 2.5, True,
                                                                       "large-v3-turbo")
    assert s.fake_stt is False and s.debug is False


def test_write_csv(tmp_path) -> None:
    rows = [TimelineRow(t_audio_ms=350.0, t_emit_ms=480.25, voiced=True, text="hola, qué tal",
                        idx_before=0, idx_after=2, advanced=True, stt_ms=130)]
    path = tmp_path / "sub" / "t.csv"
    replay.write_csv(path, rows)
    with open(path, encoding="utf-8", newline="") as fh:
        out = list(csv.DictReader(fh))
    assert list(out[0]) == list(replay.TIMELINE_FIELDS)
    assert out[0]["t_emit_ms"] == "480.2" or out[0]["t_emit_ms"] == "480.3"
    assert out[0]["text"] == "hola, qué tal" and out[0]["dropped"] == ""


# ---------------------------------------------------------------- muestras sintéticas

def test_render_text_tramos_en_utf16() -> None:
    parsed = parse_script("Hola \U0001F680mundo, adiós.\n\nOtro párrafo.")
    plan = Plan(order=[0, 1, 2, 3, 4], para_before={3})
    text, spans = render_text(plan, parsed)
    assert text == "Hola \U0001F680mundo, adiós.\n\nOtro párrafo."
    # El cohete ocupa 2 unidades UTF-16 (las posiciones de .NET cuentan así).
    assert [(s.start, s.end) for s in spans] == [(0, 4), (5, 13), (14, 20), (22, 26), (27, 35)]
    assert [s.token_idx for s in spans] == [0, 1, 2, 3, 4]


def test_map_progress_to_words() -> None:
    spans = [Span(0, 4, 0, "Hola"), Span(5, 13, 1, "\U0001F680mundo,"),
             Span(14, 20, 2, "adiós."), Span(22, 26, 3, "Otro"), Span(27, 35, 4, "párrafo.")]
    events = [
        {"t": 100, "pos": 0, "len": 4},
        {"t": 450, "pos": 7, "len": 5},      # dentro de "🚀mundo," (tras el emoji)
        {"t": 520, "pos": 10, "len": 3},     # segundo evento de la misma palabra: se funde
        {"t": 900, "pos": 14, "len": 6},
        {"t": 1700, "pos": 21, "len": 3},    # empieza en el separador y toca "Otro"
        {"t": 2500, "pos": 99, "len": 2},    # fuera de todo tramo
    ]
    words, unmapped = map_progress_to_words(spans, events)
    assert unmapped == 1
    assert [(w["token_idx"], w["start_ms"], w["end_ms"]) for w in words] == [
        (0, 100, 450), (1, 450, 900), (2, 900, 1700), (3, 1700, 1700 + synth_sample.LAST_WORD_MS)]
    assert words[1]["text"] == "\U0001F680mundo," and words[1]["pos"] == 1


def test_map_progress_relectura_repite_tokens_en_orden() -> None:
    spans = [Span(0, 3, 5, "uno"), Span(4, 7, 6, "dos"), Span(8, 11, 5, "uno"),
             Span(12, 15, 6, "dos"), Span(16, 20, 7, "tres")]
    events = [{"t": t, "pos": p, "len": 3} for t, p in
              ((0, 0), (300, 4), (700, 8), (1000, 12), (1300, 16))]
    words, unmapped = map_progress_to_words(spans, events)
    assert unmapped == 0
    assert [w["token_idx"] for w in words] == [5, 6, 5, 6, 7]
    assert [w["pos"] for w in words] == [0, 1, 2, 3, 4]


GUION_4P = (
    "Uno dos tres cuatro. Cinco seis siete.\n\n"
    "Ocho nueve diez once doce trece catorce quince.\n\n"
    "Alfa beta gama delta épsilon zeta eta theta iota. Kappa lambda.\n\n"
    "Final del guion aquí."
)


def test_build_plan_normal_y_salto() -> None:
    parsed = parse_script(GUION_4P)
    paras = synth_sample.paragraphs(parsed)
    assert [len(p) for p in paras] == [7, 8, 11, 4]
    normal = build_plan(parsed, "normal")
    assert normal.order == list(range(parsed.total_tokens)) and normal.events == []
    assert normal.para_before == {7, 15, 26}

    salto = build_plan(parsed, "salto", skip_paragraph=2)
    assert salto.order == paras[0] + paras[2] + paras[3]
    (ev,) = salto.events
    assert ev["type"] == "skip" and ev["pos"] == 7
    assert (ev["from_token"], ev["to_token"]) == (6, 15)
    assert (ev["skipped_from"], ev["skipped_to"]) == (7, 14)
    with pytest.raises(ValueError):
        build_plan(parsed, "salto", skip_paragraph=1)
    with pytest.raises(ValueError):
        build_plan(parsed, "salto", skip_paragraph=4)


def test_build_plan_relectura_cerca_de_la_mitad() -> None:
    parsed = parse_script(GUION_4P)
    plan = build_plan(parsed, "relectura", reread_words=6)
    # Frases de 6 o más palabras: "Ocho..quince." (fin 14) y "Alfa..iota." (fin 23);
    # la mitad del guion es el token 15, así que se relee la de "quince".
    (ev,) = plan.events
    assert ev["type"] == "reread" and ev["sentence_end"] == 14 and ev["words"] == 6
    assert (ev["from_token"], ev["to_token"]) == (9, 14)
    k = plan.order.index(14)
    assert plan.order[k + 1:k + 7] == [9, 10, 11, 12, 13, 14] and plan.order[k + 7] == 15
    assert ev["pos"] == k + 1
    assert len(plan.order) == parsed.total_tokens + 6

    ambos = build_plan(parsed, "salto_relectura", skip_paragraph=2, reread_words=7)
    assert [e["type"] for e in ambos.events] == ["skip", "reread"]
    assert ambos.events[1]["sentence_end"] == 23  # la frase de "quince" se saltó
    with pytest.raises(ValueError):
        build_plan(parsed, "relectura", reread_words=5)
    with pytest.raises(ValueError):
        build_plan(parsed, "silencio")


def _tone(ms: float, amp: float = 0.1) -> np.ndarray:
    n = int(ms * SR / 1000)
    return (amp * np.sin(2 * np.pi * 220 * np.arange(n) / SR)).astype(np.float32)


def _silence(ms: float) -> np.ndarray:
    return np.zeros(int(ms * SR / 1000), dtype=np.float32)


def test_trim_word_ends_corta_en_la_pausa_siguiente() -> None:
    # palabra 0-500 ms, pausa, la siguiente arranca en 980 ms aunque su evento dice 1000.
    audio = np.concatenate([_tone(500), _silence(480), _tone(420), _silence(300)])
    words = [{"start_ms": 0, "end_ms": 1000}, {"start_ms": 1000, "end_ms": 1400},
             {"start_ms": 1400, "end_ms": 1800}]
    out = synth_sample.trim_word_ends(words, audio, SR)
    assert out[0]["end_ms"] == 500
    assert out[1]["end_ms"] == 1400    # habla continua hasta el siguiente evento
    assert out[2]["end_ms"] == 1400 + synth_sample.MIN_WORD_MS  # sin voz: el mínimo
    assert words[0]["end_ms"] == 1000  # no modifica la entrada


def test_trim_word_ends_ultima_palabra_termina_con_su_voz() -> None:
    audio = np.concatenate([_tone(300), _tone(700), _silence(500)])
    words = [{"start_ms": 0, "end_ms": 300}, {"start_ms": 300, "end_ms": 700}]
    out = synth_sample.trim_word_ends(words, audio, SR)
    assert out[0]["end_ms"] == 300
    assert out[1]["end_ms"] == 1000  # la última se alarga hasta el fin de su voz


def test_make_silence_nivel_y_clics() -> None:
    audio, events = synth_sample.make_silence(10.0, clicks=3, seed=7)
    assert audio.size == 10 * SR and len(events) == 3
    level = 20 * np.log10(np.sqrt(np.mean(np.square(audio[:SR // 2], dtype=np.float64))))
    assert -51.0 < level < -49.0
    assert 20 * np.log10(np.max(np.abs(audio))) > -30  # los clics sobresalen
    again, _ = synth_sample.make_silence(10.0, clicks=3, seed=7)
    assert np.array_equal(audio, again)


def test_wav_ida_y_vuelta(tmp_path) -> None:
    audio = _tone(250, amp=0.5)
    synth_sample.write_wav(tmp_path / "a.wav", audio)
    back, rate = synth_sample.read_wav(tmp_path / "a.wav")
    assert rate == SR and back.size == audio.size
    assert np.max(np.abs(back - audio)) < 1e-3
