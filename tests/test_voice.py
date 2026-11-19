"""Bucle de voz de la Fase 2 (`backend/voice.py`) con STT, VAD, audio y matcher falsos.

Sin GPU, micrófono ni red. Las pruebas de ventanas, VAD, épocas,
desincronización, prompt y transcript llaman a `VoiceLoop._cycle()` a mano
(con un `step_ms` enorme el bucle automático nunca llega a correr), así que
son deterministas. Las de concurrencia y MIC_LOST usan el bucle real con
esperas acotadas. Ninguna espera supera los 5 s.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable

import numpy as np
import pytest

from backend import voice as voice_mod
from backend.config import Settings
from backend.fakes import FakeAudioSource, FakeSTTEngine, FakeVadGate, SegmentInfo, Transcription
from backend.voice import (
    DESYNC_MESSAGE, LISTENING_MESSAGE, MIN_WINDOW_S, PROMPT_WORDS, CycleOutput, VoiceCycle,
    VoiceLoop,
)
from tools.script_matcher import MatchResult
from tools.text_norm import to_units

SR = 16000
TIMEOUT = 5.0
MANUAL_STEP_MS = 600_000  # el bucle automático no corre: los ciclos se llaman a mano


# ---------------------------------------------------------------- dobles

class RampAudio(FakeAudioSource):
    """Fuente falsa donde cada muestra vale su índice global.

    Así la ventana que recibe el STT dice exactamente qué tramo del audio es
    (float32 representa enteros exactos hasta 2**24, unos 17 minutos).
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("realtime", False)
        kwargs.setdefault("buffer_s", 8.0)
        super().__init__(**kwargs)

    def feed(self, seconds: float | None = None, samples: np.ndarray | None = None) -> int:
        if samples is None:
            n = max(1, int(round((seconds if seconds is not None else self.block_s) * SR)))
            start = self.total_samples
            samples = np.arange(start, start + n, dtype=np.float32)
        return super().feed(samples=samples)


class RecordingSTT(FakeSTTEngine):
    """STT falso que anota cada ventana y cuántas inferencias corren a la vez."""

    def __init__(self, audio: FakeAudioSource | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.audio = audio
        # (primera muestra, última muestra, tamaño, total de la fuente al empezar)
        self.windows: list[tuple[int, int, int, int]] = []
        self.active = 0
        self.max_active = 0
        self._mx = threading.Lock()
        self.load()

    def transcribe(self, samples: np.ndarray, initial_prompt: str | None = None) -> Transcription:
        a = np.asarray(samples)
        with self._mx:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            total = self.audio.total_samples if self.audio is not None else -1
            self.windows.append((int(a[0]), int(a[-1]), int(a.size), total) if a.size
                                else (-1, -1, 0, total))
        try:
            return super().transcribe(samples, initial_prompt)
        finally:
            with self._mx:
                self.active -= 1


class StubMatcher:
    """Matcher con resultados programados y la interfaz que usa el bucle."""

    def __init__(self, results: list[MatchResult] | None = None, *,
                 before: str = "uno dos tres", near: str = "cerca del guion") -> None:
        self.results = list(results or [])
        self.before = before
        self.near = near
        self.updates: list[list[str]] = []
        self.context_calls: list[int] = []
        self.near_calls = 0
        self.current_idx = 0

    def context_before(self, n_words: int = 25) -> str:
        self.context_calls.append(n_words)
        return self.before

    def text_near(self, before: int = 10, after: int = 50) -> str:
        self.near_calls += 1
        return self.near

    def update(self, words: list[str]) -> MatchResult:
        self.updates.append(list(words))
        result = (self.results.pop(0) if self.results
                  else MatchResult(self.current_idx, False, 0.0, None))
        self.current_idx = result.new_idx
        return result


class Host:
    """Sesión mínima: época, generación, matcher y mensajes enviados."""

    def __init__(self, matcher: object) -> None:
        self.epoch = 1
        self.generation = 0
        self.matcher = matcher
        self.sent: list[dict] = []
        self.stopped: list[tuple[str, str]] = []

    def send(self, msg: dict) -> None:
        self.sent.append(msg)

    async def voice_stopped(self, voice: VoiceLoop, code: str, message: str) -> None:
        self.stopped.append((code, message))

    def msgs(self, type_: str | None = None) -> list[dict]:
        """Mensajes enviados sin `level` (o solo los de `type_`)."""
        return [m for m in self.sent
                if m["type"] != "level" and (type_ is None or m["type"] == type_)]


def run(body: Callable[[], Awaitable[None]]) -> None:
    async def main() -> None:
        await asyncio.wait_for(body(), TIMEOUT)
    asyncio.run(main())


async def start_loop(host: Host, audio: FakeAudioSource, stt: FakeSTTEngine, vad: FakeVadGate,
                     *, infer_lock: threading.Lock | None = None, **overrides) -> VoiceLoop:
    overrides.setdefault("step_ms", MANUAL_STEP_MS)
    settings = Settings(fake_stt=True, **overrides)
    voice = VoiceLoop(host, audio, stt, settings, vad=vad, infer_lock=infer_lock)
    await voice.start()
    return voice


# ---------------------------------------------------------------- ventanas

def test_ventanas_solapadas_leen_los_ultimos_window_s() -> None:
    audio, host = RampAudio(), Host(StubMatcher())
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True), window_s=1.5)
        try:
            audio.feed(2.0)    # muestras 0..31999
            await voice._cycle()
            audio.feed(0.35)   # 32000..37599
            await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    w = int(1.5 * SR)
    assert [x[:3] for x in stt.windows] == [(32000 - w, 31999, w), (37600 - w, 37599, w)]
    # La segunda ventana repite la cola de la primera (no hay bloques sueltos).
    assert stt.windows[1][0] < stt.windows[0][1]


def test_tras_reposicionar_solo_usa_audio_posterior_y_espera_1_s() -> None:
    audio, host = RampAudio(), Host(StubMatcher())
    stt, vad = RecordingSTT(audio), FakeVadGate(True)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, vad, window_s=1.5)
        try:
            audio.feed(3.0)            # 0..47999, anterior al salto
            voice.reposition()
            mark = audio.total_samples
            audio.feed(0.5)
            await voice._cycle()       # 0,5 s posteriores: no se transcribe
            assert stt.calls == 0 and vad.calls == 0
            audio.feed(0.6)
            await voice._cycle()       # 1,1 s posteriores al salto
            assert stt.windows[-1][:3] == (mark, mark + int(1.1 * SR) - 1, int(1.1 * SR))
            audio.feed(2.0)
            await voice._cycle()       # ya hay más que window_s: los últimos 1,5 s
            end = audio.total_samples
            assert stt.windows[-1][:3] == (end - int(1.5 * SR), end - 1, int(1.5 * SR))
        finally:
            await voice.stop()

    run(body)
    assert stt.calls == 2


def test_sin_audio_nuevo_no_repite_la_inferencia() -> None:
    audio, host = RampAudio(), Host(StubMatcher())
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True))
        try:
            audio.feed(2.0)
            await voice._cycle()
            await voice._cycle()  # misma ventana: mismo resultado, se salta
            audio.feed(0.35)
            await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    assert stt.calls == 2


# ---------------------------------------------------------------- VAD

def test_sin_voz_no_hay_inferencia() -> None:
    audio, host = RampAudio(), Host(StubMatcher())
    stt, vad = RecordingSTT(audio), FakeVadGate(False)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, vad)
        try:
            audio.feed(3.0)
            await voice._cycle()
            assert stt.calls == 0 and vad.calls == 1
            # El VAD recibe la ventana entera y mira solo su último segundo.
            assert (vad.last_samples, vad.last_tail_s) == (3 * SR, 1.0)
            vad.voiced = True
            audio.feed(0.35)
            await voice._cycle()
            assert stt.calls == 1
        finally:
            await voice.stop()

    run(body)
    # VAD y STT corren en un hilo de trabajo: el event loop no se bloquea.
    assert "MainThread" not in vad.threads


def test_el_vad_decide_por_la_cola_de_la_ventana() -> None:
    # Voz solo al principio de la ventana: la cola (último segundo) es silencio.
    window = np.zeros(3 * SR, dtype=np.float32)
    window[:SR] = 0.2
    stt = RecordingSTT()
    cycle = VoiceCycle(stt, FakeVadGate(), Settings(fake_stt=True))
    assert cycle.transcribe(window, prompt=None, near_text="") == CycleOutput(voiced=False)
    window[-SR // 2:] = 0.2
    assert cycle.transcribe(window, prompt=None, near_text="").voiced
    assert stt.calls == 1


# ---------------------------------------------------------------- concurrencia

def test_nunca_dos_inferencias_ni_retraso_acumulado() -> None:
    # Inferencia (250 ms) mucho más lenta que el paso (20 ms).
    audio, host = RampAudio(realtime=True, block_s=0.02), Host(StubMatcher())
    stt = RecordingSTT(audio, transcribe_delay_s=0.25)
    duration = 2.6

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True), step_ms=20)
        try:
            await asyncio.sleep(duration)
        finally:
            await voice.stop()

    run(body)
    assert stt.max_active == 1, "dos inferencias a la vez"
    calls = stt.windows
    # La primera espera a tener 1 s de audio; después, una detrás de otra.
    assert 3 <= len(calls) <= int((duration - MIN_WINDOW_S) / 0.25) + 1, calls
    for first, last, size, total_at_call in calls:
        # Siempre la ventana más reciente: termina donde estaba el audio al
        # empezar la inferencia (menos de 150 ms de diferencia), no en un
        # tramo atrasado que se haya ido acumulando.
        assert 0 <= total_at_call - (last + 1) < int(0.15 * SR), calls
        assert size == last - first + 1 <= 3 * SR
    # Entre dos inferencias pasa al menos la duración de una (no hay cola).
    ends = [last for _first, last, _size, _total in calls]
    assert all(b - a >= int(0.2 * SR) for a, b in zip(ends, ends[1:])), ends


def test_inferencia_huerfana_en_curso_salta_el_ciclo_y_reintenta() -> None:
    audio, host = RampAudio(), Host(StubMatcher())
    stt = RecordingSTT(audio)
    lock = threading.Lock()
    lock.acquire()  # la inferencia de un bucle ya detenido sigue en curso

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True), infer_lock=lock)
        try:
            audio.feed(2.0)
            await voice._cycle()
            assert stt.calls == 0, "se lanzó una segunda inferencia en paralelo"
            lock.release()
            await voice._cycle()  # sin audio nuevo, pero el ciclo anterior no corrió
            assert stt.calls == 1
        finally:
            await voice.stop()

    run(body)


# ---------------------------------------------------------------- épocas

def test_cambio_de_generacion_descarta_el_resultado() -> None:
    audio = RampAudio()
    matcher = StubMatcher([MatchResult(7, True, 0.9, "otra")])
    host = Host(matcher)
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True), debug=True)
        try:
            stt.hold()
            stt.push("hola mundo")
            audio.feed(2.0)
            cycle = asyncio.create_task(voice._cycle())
            assert await asyncio.to_thread(stt.wait_holding), "la inferencia no quedó retenida"
            host.generation += 1  # reposicionamiento durante la inferencia
            host.epoch = 2
            stt.release()
            await cycle
            assert matcher.updates == [] and host.msgs() == []
            # En la generación nueva el resultado sí se aplica, con la época nueva.
            stt.push("otra vez")
            audio.feed(0.35)
            await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    assert matcher.updates == [["otra", "vez"]]
    scrolls = host.msgs("scroll")
    assert [(m["current_idx"], m["epoch"], m["matched_word"]) for m in scrolls] == [(7, 2, "otra")]


def test_scroll_lleva_stt_ms_y_lag_desde_la_captura() -> None:
    audio = RampAudio()
    host = Host(StubMatcher([MatchResult(4, True, 0.8, "dos")]))
    stt = RecordingSTT(audio, transcribe_delay_s=0.05)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True))
        try:
            stt.push("uno dos")
            audio.feed(2.0)
            await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    (scroll,) = host.msgs("scroll")
    assert scroll["stt_ms"] >= 40
    # El lag incluye la inferencia. En Windows con Python 3.12, time.monotonic
    # (marca de captura) tiene una resolución de ~16 ms.
    assert scroll["lag_ms"] >= scroll["stt_ms"] - 20
    assert (scroll["current_idx"], scroll["confidence"], scroll["resync"]) == (4, 0.8, False)


# ---------------------------------------------------------------- desincronización

def test_desync_y_listening_solo_en_las_transiciones() -> None:
    audio = RampAudio()
    results = [
        MatchResult(3, True, 0.9, "tres"),
        MatchResult(3, False, 0.0, None, desync=True),
        MatchResult(3, False, 0.0, None, desync=True),           # sigue: no se repite
        MatchResult(1, False, 0.9, "uno", resync=True),           # resync hacia atrás
        MatchResult(2, True, 0.9, "dos"),
        MatchResult(2, False, 0.0, None, desync=True),
    ]
    host = Host(StubMatcher(results))
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True))
        try:
            audio.feed(1.65)
            for i in range(len(results)):
                stt.push(f"palabra numero {i}")
                audio.feed(0.35)
                await voice._cycle()
            voice.reposition()  # el usuario elige la posición: sale de "buscando"
            voice.reposition()  # sin desync: no se repite
        finally:
            await voice.stop()

    run(body)
    seen = [(m["type"], m.get("state") or m.get("current_idx"), m.get("resync"))
            for m in host.msgs()]
    assert seen == [
        ("scroll", 3, False),
        ("status", "desync", None),
        ("scroll", 1, True),
        ("status", "listening", None),
        ("scroll", 2, False),
        ("status", "desync", None),
        ("status", "listening", None),
    ]
    statuses = host.msgs("status")
    assert statuses[0] == {"type": "status", "state": "desync", "code": None,
                           "message": DESYNC_MESSAGE}
    assert statuses[1]["message"] == LISTENING_MESSAGE and statuses[1]["code"] is None


# ---------------------------------------------------------------- prompt

@pytest.mark.parametrize("prompt_context", [False, True])
def test_prompt_solo_con_prompt_context(prompt_context: bool) -> None:
    audio = RampAudio()
    matcher = StubMatcher(before="las palabras anteriores del guion")
    host = Host(matcher)
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True),
                                 prompt_context=prompt_context)
        try:
            audio.feed(2.0)
            await voice._cycle()
            matcher.before = ""  # al principio del guion no hay contexto
            audio.feed(0.35)
            await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    if prompt_context:
        assert stt.prompts == ["las palabras anteriores del guion", None]
        assert matcher.context_calls == [PROMPT_WORDS, PROMPT_WORDS] and PROMPT_WORDS == 25
    else:
        assert stt.prompts == [None, None]
        assert matcher.context_calls == []


def test_voice_cycle_respeta_prompt_context() -> None:
    stt = RecordingSTT()
    window = np.full(2 * SR, 0.2, dtype=np.float32)
    VoiceCycle(stt, FakeVadGate(True), Settings()).transcribe(
        window, prompt="contexto", near_text="")
    VoiceCycle(stt, FakeVadGate(True), Settings(prompt_context=True)).transcribe(
        window, prompt="contexto", near_text="")
    assert stt.prompts == [None, "contexto"]


# ---------------------------------------------------------------- transcript y filtro

@pytest.mark.parametrize("debug", [False, True])
def test_transcript_solo_con_debug(debug: bool) -> None:
    audio = RampAudio()
    matcher = StubMatcher([MatchResult(2, True, 0.9, "mundo")])
    host = Host(matcher)
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True), debug=debug)
        try:
            stt.push("Hola mundo", "¡Gracias por ver!", "")
            audio.feed(1.3)
            for _ in range(3):
                audio.feed(0.35)
                await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    transcripts = [m["text"] for m in host.msgs("transcript")]
    if debug:
        assert transcripts == ["Hola mundo", "[descartado: blacklist] ¡Gracias por ver!"]
    else:
        assert transcripts == []
    # Lo descartado y lo vacío nunca llegan al matcher.
    assert matcher.updates == [["hola", "mundo"]]
    assert [m["current_idx"] for m in host.msgs("scroll")] == [2]


def test_frase_de_la_lista_negra_que_esta_en_el_guion_no_se_descarta() -> None:
    audio = RampAudio()
    matcher = StubMatcher(near="y eso es todo muchas gracias por ver este video")
    host = Host(matcher)
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True))
        try:
            stt.push("Gracias por ver.")
            audio.feed(2.0)
            await voice._cycle()
        finally:
            await voice.stop()

    run(body)
    assert matcher.near_calls == 1
    assert matcher.updates == [["gracias", "por", "ver"]]


def test_voice_cycle_filtra_y_normaliza() -> None:
    stt = RecordingSTT()
    cycle = VoiceCycle(stt, FakeVadGate(True), Settings())
    matcher = StubMatcher([MatchResult(9, True, 0.9, "RTX-5070")])
    window = np.full(2 * SR, 0.2, dtype=np.float32)

    # Ventana corta: ni VAD ni STT.
    assert cycle.transcribe(window[:SR - 1], prompt=None, near_text="") == CycleOutput(False)
    assert stt.calls == 0

    stt.push("La RTX-5070 funciona.")
    out = cycle.transcribe(window, prompt=None, near_text="")
    assert (out.voiced, out.dropped, out.text) == (True, None, "La RTX-5070 funciona.")
    assert out.words == to_units("La RTX-5070 funciona.")
    assert out.stt_ms >= 0
    result = VoiceCycle.apply(out, matcher)
    assert result is not None and result.new_idx == 9
    assert matcher.updates == [out.words]

    stt.push("de")  # una sola stopword
    out = cycle.transcribe(window, prompt=None, near_text="")
    assert (out.voiced, out.dropped, out.words) == (True, "stopword", [])
    assert VoiceCycle.apply(out, matcher) is None

    # Segmento con métricas de silencio (alucinación típica): no llega al matcher.
    stt.push(Transcription("Hola mundo", [SegmentInfo("Hola mundo", no_speech_prob=0.9,
                                                       avg_logprob=-1.5)]))
    out = cycle.transcribe(window, prompt=None, near_text="")
    assert (out.voiced, out.dropped, out.text, out.words) == (True, "no_speech", "Hola mundo", [])
    assert VoiceCycle.apply(out, matcher) is None

    assert VoiceCycle.apply(CycleOutput(voiced=False), matcher) is None
    assert matcher.updates == [to_units("La RTX-5070 funciona.")]


# ---------------------------------------------------------------- micrófono

def test_mic_lost_sigue_funcionando(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_mod, "MIC_TIMEOUT_S", 0.3)
    audio, host = RampAudio(realtime=True, block_s=0.02), Host(StubMatcher())
    stt = RecordingSTT(audio)

    async def body() -> None:
        voice = await start_loop(host, audio, stt, FakeVadGate(True), step_ms=20)
        try:
            await asyncio.sleep(0.1)
            audio.stall()
            deadline = time.monotonic() + 3.0
            while not host.stopped and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        finally:
            await voice.stop()

    run(body)
    assert [code for code, _message in host.stopped] == ["MIC_LOST"]
    assert "dejó de enviar audio" in host.stopped[0][1]
    assert any(m["type"] == "level" for m in host.sent), "el medidor de nivel no informó"
