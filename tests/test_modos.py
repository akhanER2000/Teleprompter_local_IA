"""Hito 84 (T4): modos Voz (con frenos) y Asistido de punta a punta en el servidor."""
from __future__ import annotations

import time

from backend.fakes import FakeAudioSource, FakeVadGate
from test_protocol import WORDS, make  # noqa: F401  (fixture)


def _start(c, **extra) -> None:
    c.send(action="start", idx=120, seq=2, **extra)
    c.start_reply()


def test_voz_con_frenos_avanza_de_a_una_y_sin_pasar_lo_dicho(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        _start(c, mode="voz", brakes={"confirm_words": 3, "max_ahead": 6, "max_wps": 10})
        said = WORDS[120:126]
        # Un solo ciclo no confirma nada; el segundo igual, sí.
        h.stt.push(" ".join(said))
        h.stt.push(" ".join(said))
        idxs = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not idxs or idxs[-1] < 126):
            for m in c.drain(0.3):
                if m["type"] == "scroll":
                    idxs.append(m["current_idx"])
        assert idxs, "no avanzó"
        steps = [b - a for a, b in zip([120] + idxs, idxs)]
        assert max(steps) == 1, f"saltos: {steps}"          # de a una palabra
        assert idxs[-1] <= 126                               # nunca pasa lo dicho
        assert h.session.shown_idx() == idxs[-1]


def test_voz_un_solo_ciclo_no_mueve_antes_del_plazo(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        _start(c, mode="voz", brakes={"confirm_hold_s": 3.0})
        h.stt.push(" ".join(WORDS[120:126]))
        msgs = c.drain(1.5)
        assert not [m for m in msgs if m["type"] == "scroll"], msgs


def test_frenos_invalidos_se_rechazan(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        c.send(action="start", idx=0, seq=2, mode="voz", brakes={"max_ahead": 99})
        err = c.wait_type("status", state="error")
        assert err["code"] == "BAD_REQUEST" and "max_ahead" in err["message"]
        c.send(action="start", idx=0, seq=3, mode="turbo")
        assert c.wait_type("status", state="error")["code"] == "BAD_REQUEST"


def test_asistido_informa_si_hay_voz_y_no_mueve_el_cursor(make) -> None:
    vad = FakeVadGate()
    h = make(vad=vad, audio=FakeAudioSource(block_s=0.02))
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        _start(c, mode="asistido")
        acts = [m for m in c.drain(1.5) if m["type"] in ("activity", "scroll")]
        assert acts and all(m["type"] == "activity" for m in acts), acts
        assert all(isinstance(m["speaking"], bool) and m["silence_ms"] >= 0 for m in acts)
        assert h.stt.calls == 0   # Asistido no transcribe


def test_cambiar_de_modo_reinicia_el_bucle(make) -> None:
    h = make()
    with h.connect() as c:
        c.hello()
        c.load(seq=1)
        _start(c, mode="asistido")
        first = h.session.voice
        assert first.mode == "asistido"
        _start(c, mode="voz")
        assert h.session.voice is not first and h.session.voice.mode == "voz"
        assert h.session.voice.brakes is not None
