"""Frenos del modo Voz (hito 84, T4): reglas una a una y una lectura de prueba simulada.

La lectura de prueba recorre `guiones/ejemplo.txt` con el matcher real y
ventanas solapadas de 3 s, como el bucle en vivo, y con un Whisper que a
veces "completa" la frase con palabras que el orador aún no dijo (el defecto
que medía V0). Con los frenos, el cursor visible nunca queda más de 6
palabras por delante de lo dicho y nunca salta más de 3 en una emisión. Sin
ellos (`test_sin_frenos_la_lectura_se_adelanta`) la misma lectura se adelanta:
así se sabe que la prueba puede fallar.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from backend.config import ROOT
from tools.script_matcher import ScriptMatcher
from tools.script_parser import parse_script
from tools.voice_brakes import BrakeParams, VoiceBrakes, is_sentence_end


@dataclass
class Tok:
    text: str
    units: list[str]


def toks(*texts: str) -> list[Tok]:
    return [Tok(t, [] if t in ("—", "-") else [t.strip(".,?!").lower()]) for t in texts]


# ---------------------------------------------------------------- reglas

def test_sin_confirmacion_no_se_mueve() -> None:
    b = VoiceBrakes(toks(*"uno dos tres cuatro cinco seis".split()))
    b.observe(3, run=2, now=0.0)
    assert b.goal == 0
    assert b.tick(10.0) is None


def confirm(b: VoiceBrakes, idx: int, run: int, now: float = 0.0) -> None:
    """Dos ciclos seguidos que respaldan la misma posición."""
    b.observe(idx, run, now=now)
    b.observe(idx, run, now=now)


def test_un_solo_ciclo_no_confirma() -> None:
    b = VoiceBrakes(toks(*"uno dos tres cuatro cinco seis".split()))
    b.observe(3, run=5, now=0.0)
    assert b.goal == 0
    b.observe(2, run=1, now=0.1)          # el ciclo siguiente no respalda: se olvida
    b.observe(3, run=5, now=0.2)
    assert b.goal == 0


def test_dos_ciclos_toman_la_menor() -> None:
    b = VoiceBrakes(toks(*"uno dos tres cuatro cinco seis".split()))
    b.observe(5, run=5, now=0.0)          # este ciclo completó la frase de más
    b.observe(3, run=3, now=0.1)
    assert b.goal == 3


def test_confirmacion_de_tres_mueve_paso_a_paso() -> None:
    b = VoiceBrakes(toks(*"uno dos tres cuatro cinco seis".split()), BrakeParams(max_wps=10))
    confirm(b, 3, 3)
    assert b.goal == 3
    seen = [b.tick(t / 10) for t in range(1, 6)]
    assert [x for x in seen if x is not None] == [1, 2, 3]


def test_adelanto_maximo_sobre_la_ultima_confirmada() -> None:
    words = [f"p{i}" for i in range(40)]
    b = VoiceBrakes(toks(*words), BrakeParams(max_ahead=6, sentence_guard=False))
    confirm(b, 20, 12)
    assert b.goal == 6
    b.observe(20, run=12, now=0.1)
    assert b.goal == 12


def test_velocidad_maxima_reparte_el_salto() -> None:
    b = VoiceBrakes(toks(*[f"p{i}" for i in range(30)]), BrakeParams(max_wps=2.0, max_ahead=20))
    confirm(b, 10, 10)
    t, moves = 0.0, []
    while t < 3.05:
        t += 0.1
        idx = b.tick(t)
        if idx is not None:
            moves.append((round(t, 1), idx))
    # 2 palabras por segundo: en 3 s, 6 pasos de una palabra, nunca dos de golpe.
    assert [i for _, i in moves] == [1, 2, 3, 4, 5, 6]
    assert all(b2 - b1 >= 0.45 for (b1, _), (b2, _) in zip(moves, moves[1:]))


def test_parado_no_acumula_rafaga() -> None:
    b = VoiceBrakes(toks(*[f"p{i}" for i in range(30)]), BrakeParams(max_wps=2.0))
    assert b.tick(60.0) is None          # un minuto parado
    confirm(b, 5, 5, now=60.0)
    assert b.tick(60.1) is None          # sin crédito acumulado: espera su turno
    assert b.tick(60.6) == 1


def test_no_da_por_terminada_una_frase_no_dicha() -> None:
    b = VoiceBrakes(toks("Hola", "a", "todos.", "Hoy", "vamos"), BrakeParams(max_wps=10))
    confirm(b, 3, 3)                      # el alineamiento cierra en "todos."
    assert b.goal == 2                    # se queda EN la última palabra de la frase
    b.observe(3, run=3, now=0.4)          # otra confirmación igual: ahora sí
    assert b.goal == 3
    b2 = VoiceBrakes(toks("Hola", "a", "todos.", "Hoy", "vamos"), BrakeParams(max_wps=10))
    confirm(b2, 4, 4)                     # ya empezó la frase siguiente: pasa
    assert b2.goal == 4


def test_frase_desactivada_pasa_de_una() -> None:
    b = VoiceBrakes(toks("Hola", "a", "todos.", "Hoy"), BrakeParams(sentence_guard=False))
    confirm(b, 3, 3)
    assert b.goal == 3


def test_resync_salta_sin_frenos_y_reset() -> None:
    b = VoiceBrakes(toks(*[f"p{i}" for i in range(80)]))
    assert b.observe(60, run=4, resync=True, now=0.0) is True
    assert (b.displayed, b.confirmed, b.goal) == (60, 60, 60)
    b.reset(10)
    assert (b.displayed, b.confirmed, b.goal) == (10, 10, 10)


def test_salta_signos_sin_unidades() -> None:
    b = VoiceBrakes(toks("uno", "—", "dos", "tres", "cuatro"), BrakeParams(max_wps=10))
    confirm(b, 4, 3)
    out = [b.tick(t / 10) for t in range(1, 6)]
    assert [x for x in out if x is not None] == [2, 3, 4]


@pytest.mark.parametrize("bad", [{"confirm_words": 0}, {"max_ahead": 31}, {"max_wps": 0.1},
                                 {"max_wps": True}, {"sentence_guard": 1}])
def test_parametros_fuera_de_rango(bad: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        BrakeParams(**bad)


def test_fin_de_frase() -> None:
    assert is_sentence_end("todos.") and is_sentence_end("¿qué tal?") and is_sentence_end("fin…")
    assert not is_sentence_end("Hola,") and not is_sentence_end("datos")


# ---------------------------------------------------------------- lectura de prueba

STEP_S = 0.35          # ciclo del bucle de voz (backend/config.py: step_ms)
WINDOW_S = 3.0         # ventana de audio
TICK_S = 0.1           # tick de los frenos (backend/voice.py: BRAKE_TICK_S)


def _lectura(brakes: BrakeParams | None, *, seed: int = 84, wps: float = 2.5,
             complete_p: float = 0.35, complete_max: int = 8):
    """Simula una lectura y devuelve (adelanto máximo, salto máximo por emisión, índice final, total).

    El orador dice una palabra cada 1/wps s, con 1,5 s de pausa tras cada
    punto. En cada ciclo Whisper transcribe lo dicho en los últimos 3 s y,
    con probabilidad `complete_p`, le añade de 1 a `complete_max` palabras
    que todavía NO se dijeron (completa la frase).
    """
    text = (ROOT / "guiones" / "ejemplo.txt").read_text(encoding="utf-8")
    parsed = parse_script(text)
    tokens = parsed.tokens
    spoken = [i for i, t in enumerate(tokens) if t.units]
    times: list[float] = []
    t = 0.5
    for i in spoken:
        times.append(t)
        t += 1.0 / wps
        if is_sentence_end(tokens[i].text):
            t += 1.5
    end_t = t + 3.0
    rng = random.Random(seed)
    matcher = ScriptMatcher(tokens)
    b = VoiceBrakes(tokens, brakes) if brakes is not None else None
    shown = 0
    worst_ahead = 0
    worst_jump = 0

    def said_upto(now: float) -> int:
        """Índice del token siguiente al último ya dicho entero."""
        k = sum(1 for x in times if x + 1.0 / wps <= now)
        return spoken[k - 1] + 1 if k else 0

    def emit(idx: int, now: float) -> None:
        nonlocal shown, worst_ahead, worst_jump
        worst_jump = max(worst_jump, idx - shown)
        shown = idx
        # Adelanto en palabras con unidades: cuántas de las que el cursor ya pasó no se dijeron.
        ahead = sum(1 for i in spoken if said_upto(now) <= i < idx)
        worst_ahead = max(worst_ahead, ahead)

    now, next_cycle = 0.0, STEP_S
    while now < end_t:
        now = round(now + TICK_S, 3)
        if b is not None:
            idx = b.tick(now)
            if idx is not None:
                emit(idx, now)
        if now + 1e-9 < next_cycle:
            continue
        next_cycle += STEP_S
        heard = [k for k, x in enumerate(times) if now - WINDOW_S <= x and x + 1.0 / wps <= now]
        if not heard:
            continue
        units = [u for k in heard for u in tokens[spoken[k]].units]
        if rng.random() < complete_p:   # Whisper completa con lo que aún no se dijo
            nxt = heard[-1] + 1
            units += [u for k in range(nxt, min(len(spoken), nxt + rng.randint(1, complete_max)))
                      for u in tokens[spoken[k]].units]
        res = matcher.update(units)
        if b is None:
            if res.advanced or res.resync:
                emit(res.new_idx, now)
            continue
        if b.observe(res.aligned_idx, res.run, resync=res.resync, now=now):
            emit(b.displayed, now)
    return worst_ahead, worst_jump, shown, len(tokens)


@pytest.mark.parametrize("seed", [84, 1, 2, 3, 4])
def test_lectura_de_prueba_con_frenos(seed: int) -> None:
    ahead, jump, final, total = _lectura(BrakeParams(), seed=seed)
    assert ahead <= 6, f"el cursor quedó {ahead} palabras por delante de lo dicho"
    assert jump <= 3, f"salto de {jump} palabras en una emisión"
    assert final == total, f"no llegó al final: {final} de {total}"


def test_sin_frenos_la_lectura_se_adelanta() -> None:
    """Control negativo: la misma lectura sin frenos se adelanta y salta (la prueba sabe fallar)."""
    worst = max(_lectura(None, seed=s)[:2] for s in (84, 1, 2, 3, 4))
    assert worst[0] > 6 or worst[1] > 3, worst


# ---------------------------------------------------------------- plazo de confirmación

def test_observacion_sin_contradiccion_se_confirma_pasado_el_plazo() -> None:
    """La última frase antes de una pausa: Whisper calla y nadie la respalda dos veces."""
    b = VoiceBrakes(toks(*[f"p{i}" for i in range(30)]), BrakeParams(max_wps=10, confirm_hold_s=0.8))
    b.observe(5, 5, now=10.0)
    assert b.tick(10.5) is None and b.confirmed == 0      # dentro del plazo, nada
    b.tick(10.85)
    assert b.confirmed == 5                               # pasado el plazo, se da por dicha


def test_un_ciclo_debil_anula_el_plazo() -> None:
    b = VoiceBrakes(toks(*[f"p{i}" for i in range(30)]), BrakeParams(confirm_hold_s=0.8))
    b.observe(5, 5, now=10.0)
    b.observe(6, 1, now=10.3)                             # contradicción: alineamiento flojo
    b.tick(11.5)
    assert b.confirmed == 0


def test_una_observacion_menor_manda_sobre_la_pendiente() -> None:
    """Una completación de Whisper (8) seguida de la posición real (4): gana la real."""
    b = VoiceBrakes(toks(*[f"p{i}" for i in range(30)]), BrakeParams(max_ahead=20, confirm_hold_s=0.8))
    b.observe(8, 8, now=10.0)
    b.observe(4, 4, now=10.35)
    assert b.confirmed == 4
    b.tick(10.5)
    assert b.confirmed == 4                               # el 4 recién observado aún no cumple el plazo
