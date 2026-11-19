"""Replay: mide la sincronización por voz sobre un WAV, sin micrófono ni navegador.

Uso:
  .venv312\\Scripts\\python.exe tools\\replay.py --wav tests\\fixtures\\audio\\lectura.wav --script guiones\\ejemplo.txt
  .venv312\\Scripts\\python.exe tools\\replay.py --wav tests\\fixtures\\audio_synth\\ejemplo_salto.wav ^
      --ref tests\\fixtures\\audio_synth\\ejemplo_salto.ref.json --csv salto.csv --json salto.json
  .venv312\\Scripts\\python.exe tools\\replay.py --wav a.wav b.wav --ref auto --script guiones\\ejemplo.txt ^
      --sweep step_ms=300,350,400 window_s=2.5,3 prompt_context=0,1 model=medium,large-v3-turbo

Alimenta el pipeline real: decodifica el WAV con `tools.audio_file_source.decode_file`,
carga los tokens de `parse_script` en un `ScriptMatcher`, y en cada ciclo pasa la
ventana por `backend.voice.VoiceCycle` (VAD de Silero, Whisper con `STTEngine`, filtro
de alucinaciones y `to_units`) y aplica el resultado al matcher. Es la única
importación de `tools/` hacia `backend/`: así el replay mide exactamente el ciclo del
bucle en vivo en lugar de una copia que podría divergir.

Reloj virtual (igual que el bucle en vivo): un ciclo en el instante de audio t usa la
ventana `audio[max(0, t - window_s) : t]` (con t recortado al último bloque de captura
de `--block-ms`). Si la inferencia tarda d ms (medidos de verdad), el resultado se
emite en t + d y el ciclo siguiente empieza en max(t + step, t + d): los ciclos que no
caben se saltan, nunca se encolan. Al final del WAV se agregan `--pad-s` segundos de
silencio, como el micrófono que sigue abierto tras la última palabra.

Referencia (cuándo se dijo cada token): `--ref archivo.ref.json` (por ejemplo, el de
`tools/synth_sample.py`), `--ref auto` (`<wav>.ref.json` junto al WAV si existe) o, sin
`--ref`, un alineamiento offline: Whisper transcribe el WAV completo con
`word_timestamps=True` (`beam_size` 5, idioma fijo) y `script_matcher.local_align`
alinea esas palabras con las unidades del guion; el tiempo de cada token es el de las
palabras alineadas con sus unidades. `--save-ref` guarda esa referencia.

Métricas (PROMPT_MAESTRO_CORRECCIONES.md, Fase 2, punto 8):
- retraso: para cada token con unidades que se dijo, desde el fin de la palabra en la
  referencia hasta la primera emisión con índice mayor que el token (p50, p95, máximo y
  tokens nunca pasados);
- saltos falsos: emisiones cuyo índice supera en más de 5 la posición de referencia en
  ese instante (1 + el mayor token cuyo inicio ya ocurrió);
- retrocesos: emisiones que bajan el índice (aparte, las que son resincronización);
- cobertura: tokens dichos que el índice llegó a pasar sobre tokens dichos;
- resincronización: para cada salto hacia adelante de la referencia (más de 10
  tokens), tiempo desde que se empieza a decir el primer token tras el salto hasta la
  primera emisión con índice mayor o igual que ese token;
- avances totales (en una grabación de silencio deben ser 0).

Las salidas `--csv` y `--json` contienen el texto transcrito: con grabaciones de voz
del usuario van solo a `tests/fixtures/audio/` (ignorada por git).
"""
from __future__ import annotations

import argparse
import bisect
import csv
import dataclasses
import itertools
import json
import logging
import math
import os
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # ejecutado como script: `tools\replay.py`
    sys.path.insert(0, str(ROOT))
# Sin red: antes de que cualquier import cargue faster_whisper o huggingface_hub.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from tools.script_parser import ParsedScript, Token, parse_script  # noqa: E402
from tools.text_norm import to_units  # noqa: E402

log = logging.getLogger("teleprompter.replay")

SAMPLE_RATE = 16000
DEFAULT_PAD_S = 1.5          # silencio tras el WAV (el micrófono sigue abierto)
DEFAULT_BLOCK_MS = 100       # bloques de la captura en vivo (tools.audio_capture.BLOCK_S)
PROMPT_WORDS = 25            # respaldo si backend.voice no define PROMPT_WORDS

FALSE_JUMP_TOKENS = 5        # un avance que supera la referencia en más de esto es falso
SKIP_MIN_TOKENS = 10         # un salto de la referencia mayor que esto es un salto de párrafo

REF_BEAM_SIZE = 5
REF_MIN_SCORE = 3.0          # alineamiento offline aceptable (igual que min_score del matcher)
REF_MIN_CONTENT = 2          # y con al menos 2 palabras de contenido
REF_INTERP_MAX_GAP = 3       # tokens sin alinear entre dos alineados que se interpolan
REF_INTERP_WORD_MS = 500.0   # duración máxima de cada token interpolado
FULL_ALIGN_MAX_CELLS = 1_500_000  # palabras x unidades para alinear de una vez
CHUNK_WORDS = 120            # si no, por tramos de palabras...
CHUNK_UNITS = 1200           # ...contra una ventana de unidades...
CHUNK_BACK_UNITS = 40        # ...que empieza un poco antes de lo ya alineado

# Objetivos de la Fase 2 (PROMPT_MAESTRO_CORRECCIONES.md).
TARGET_P50_MS = 700
TARGET_P95_MS = 1500
TARGET_RESYNC_MS = 3000

# Campos de ReplayConfig que no son parámetros del matcher (para --sweep).
_CONFIG_KEYS = ("step_ms", "window_s", "prompt_context", "model", "device", "compute",
                "vad_threshold", "language", "pad_s", "block_ms")


# ---------------------------------------------------------------- datos

@dataclass
class RefWord:
    """Un token dicho: cuándo empieza y termina en el audio."""

    token_idx: int
    text: str
    start_ms: float
    end_ms: float


@dataclass
class Reference:
    """Tokens dichos en orden de lectura (una relectura repite tokens)."""

    words: list[RefWord] = field(default_factory=list)
    source: str = ""
    events: list[dict] = field(default_factory=list)

    def to_json(self, script: str | None = None) -> dict:
        return {
            "script": script, "sample_rate": SAMPLE_RATE, "source": self.source,
            "words": [{"token_idx": w.token_idx, "text": w.text,
                       "start_ms": int(round(w.start_ms)), "end_ms": int(round(w.end_ms))}
                      for w in self.words],
            "events": self.events,
        }


@dataclass
class TimelineRow:
    """Un ciclo del bucle simulado."""

    t_audio_ms: float               # instante de audio en que empieza el ciclo
    t_emit_ms: float                # instante en que su resultado se aplica (t + inferencia)
    voiced: bool = False            # se llamó al STT
    dropped: str | None = None      # motivo del filtro de alucinaciones
    text: str = ""
    idx_before: int = 0
    idx_after: int = 0
    advanced: bool = False
    resync: bool = False
    desync: bool = False
    stt_ms: int = 0


TIMELINE_FIELDS = tuple(f.name for f in dataclasses.fields(TimelineRow))


@dataclass
class ReplayConfig:
    """Configuración de una corrida (lo que se barre con --sweep)."""

    step_ms: int = 350
    window_s: float = 3.0
    prompt_context: bool = False
    model: str = "medium"
    device: str = "cuda"
    compute: str = "float16"
    vad_threshold: float = 0.5
    language: str = "es"
    pad_s: float = DEFAULT_PAD_S
    block_ms: int = DEFAULT_BLOCK_MS
    matcher_params: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> ReplayConfig:
        """Toma los valores de `backend.config.Settings`; los `None` de overrides se ignoran."""
        cfg = cls(step_ms=settings.step_ms, window_s=settings.window_s,
                  prompt_context=settings.prompt_context, model=settings.model_size,
                  device=settings.device, compute=settings.compute_type,
                  vad_threshold=settings.vad_threshold, language=settings.language)
        return dataclasses.replace(cfg, **{k: v for k, v in overrides.items() if v is not None})

    def to_settings(self, base: Any = None) -> Any:
        from backend.config import Settings

        base = Settings() if base is None else base
        return dataclasses.replace(
            base, step_ms=int(self.step_ms), window_s=float(self.window_s),
            prompt_context=bool(self.prompt_context), model_size=self.model,
            device=self.device, compute_type=self.compute,
            vad_threshold=float(self.vad_threshold), language=self.language,
            fake_stt=False, debug=False)

    def label(self) -> str:
        parts = [f"step={self.step_ms}", f"win={self.window_s:g}",
                 f"prompt={int(bool(self.prompt_context))}", self.model]
        if self.device != "cuda" or self.compute != "float16":
            parts.append(f"{self.device}/{self.compute}")
        if self.vad_threshold != 0.5:
            parts.append(f"vad={self.vad_threshold:g}")
        parts += [f"{k}={v:g}" for k, v in sorted(self.matcher_params.items())]
        return " ".join(parts)


@dataclass
class ReplayResult:
    wav: str
    config: ReplayConfig
    timeline: list[TimelineRow]
    reference: Reference
    metrics: dict[str, Any]
    total_tokens: int
    duration_ms: float


# ---------------------------------------------------------------- referencia

def load_reference(path: str | os.PathLike) -> Reference:
    """Lee un `.ref.json` (formato de tools/synth_sample.py o de --save-ref)."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    ref = as_reference(data)
    ref.source = f"archivo {Path(path).name}"
    return ref


def as_reference(ref: Any) -> Reference:
    """Normaliza Reference, un dict con "words", una lista de palabras o None."""
    if isinstance(ref, Reference):
        return ref
    if ref is None:
        return Reference(source="vacía")
    events: list[dict] = []
    words_raw: Iterable[Any]
    if isinstance(ref, Mapping):
        words_raw = ref.get("words") or []
        events = list(ref.get("events") or [])
        source = str(ref.get("source") or "")
    else:
        words_raw, source = ref, ""
    words = []
    for w in words_raw:
        if isinstance(w, RefWord):
            words.append(w)
        else:
            words.append(RefWord(int(w["token_idx"]), str(w.get("text", "")),
                                 float(w["start_ms"]), float(w["end_ms"])))
    return Reference(words=words, source=source, events=events)


def _flatten_units(tokens: Sequence[Token]) -> tuple[list[str], list[int]]:
    units: list[str] = []
    owners: list[int] = []
    for tok in tokens:
        for unit in tok.units:
            units.append(unit)
            owners.append(tok.idx)
    return units, owners


def _accepted(al: Any) -> bool:
    return bool(al.pairs) and al.score >= REF_MIN_SCORE and al.content_matches >= REF_MIN_CONTENT


def align_pairs(words: Sequence[str], units: Sequence[str]) -> list[tuple[int, int]]:
    """Pares (palabra, unidad) del alineamiento de la transcripción completa con el guion.

    Usa `script_matcher.local_align` (el mismo alineador del matcher). Con
    secuencias grandes alinea por tramos de `CHUNK_WORDS` palabras contra una
    ventana de unidades que sigue a lo ya alineado; los pares son siempre
    crecientes en ambas secuencias (una relectura queda sin alinear).
    """
    from tools.script_matcher import local_align

    if not words or not units:
        return []
    if len(words) * len(units) <= FULL_ALIGN_MAX_CELLS:
        al = local_align(words, units)
        return list(al.pairs) if _accepted(al) else []
    pairs: list[tuple[int, int]] = []
    w0 = 0
    u_lo = 0
    last_u = -1
    while w0 < len(words):
        chunk = words[w0:w0 + CHUNK_WORDS]
        u_hi = min(len(units), u_lo + CHUNK_UNITS)
        al = local_align(chunk, units[u_lo:u_hi])
        if not _accepted(al):
            w0 += max(1, len(chunk) // 2)
            continue
        new = [(w0 + wi, u_lo + ui) for wi, ui in al.pairs if u_lo + ui > last_u]
        pairs.extend(new)
        if new:
            last_u = new[-1][1]
            u_lo = max(0, last_u - CHUNK_BACK_UNITS)
        w0 += max(wi for wi, _ui in al.pairs) + 1
    return pairs


def prefer_first_occurrence(words: Sequence[str],
                            pairs: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Mueve a la primera lectura los pares que el alineador asignó a una relectura.

    En una relectura ("... X Y Z X Y Z D ...") el alineamiento óptimo puede
    tomar la segunda copia y dejar la primera como hueco: los dos tienen el
    mismo puntaje. Para la referencia importa cuándo se dijo cada token por
    primera vez, así que si antes de un bloque de pares hay L palabras sin
    alinear y las palabras del bloque se repiten exactas L posiciones antes
    (dentro del hueco), esos pares se corren L palabras hacia atrás.
    """
    out = list(pairs)
    prev_wi = -1
    k = 0
    while k < len(out):
        wi = out[k][0]
        gap = wi - prev_wi - 1
        if gap > 0:
            m = k
            while (m < len(out) and out[m][0] - gap > prev_wi
                   and words[out[m][0] - gap] == words[out[m][0]]):
                m += 1
            for j in range(k, m):
                out[j] = (out[j][0] - gap, out[j][1])
        prev_wi = out[k][0]
        k += 1
    return out


def reference_from_words(words: Sequence[tuple[str, float, float]],
                         tokens: Sequence[Token]) -> list[RefWord]:
    """Tiempos por token a partir de palabras con marcas de tiempo (texto, inicio s, fin s).

    Cada palabra se normaliza con `to_units`; si da varias unidades ("5070"
    da cinco, mil, setenta), su intervalo se reparte entre ellas en
    proporción a su largo. Cada unidad alineada se asigna a su token: inicio
    y fin del token son el menor inicio y el mayor fin de sus unidades
    alineadas (de la primera lectura, ver `prefer_first_occurrence`). Los
    huecos de hasta `REF_INTERP_MAX_GAP` tokens con unidades entre dos tokens
    alineados (una palabra mal reconocida) se interpolan, con a lo sumo
    `REF_INTERP_WORD_MS` por token.
    """
    units, owners = _flatten_units(tokens)
    w_units: list[str] = []
    w_times: list[tuple[float, float]] = []
    for text, start, end in words:
        parts = to_units(text)
        t0, t1 = float(start) * 1000.0, max(float(start), float(end)) * 1000.0
        total = sum(len(u) for u in parts) or 1
        acc = 0
        for unit in parts:
            w_units.append(unit)
            w_times.append((t0 + (t1 - t0) * acc / total,
                            t0 + (t1 - t0) * (acc + len(unit)) / total))
            acc += len(unit)
    spans: dict[int, list[float]] = {}
    for wi, ui in prefer_first_occurrence(w_units, align_pairs(w_units, units)):
        tok = owners[ui]
        start, end = w_times[wi]
        cur = spans.get(tok)
        if cur is None:
            spans[tok] = [start, end]
        else:
            cur[0], cur[1] = min(cur[0], start), max(cur[1], end)
    out: list[RefWord] = []
    aligned = sorted(spans)
    for k, tok in enumerate(aligned):
        if k:
            prev = aligned[k - 1]
            missing = [i for i in range(prev + 1, tok) if tokens[i].units]
            if 0 < len(missing) <= REF_INTERP_MAX_GAP:
                t0, t1 = spans[prev][1], max(spans[prev][1], spans[tok][0])
                # Tope por palabra: si entre ambos hubo una pausa o una relectura,
                # el token perdido va justo después del anterior.
                slot = min((t1 - t0) / len(missing), REF_INTERP_WORD_MS)
                for n, i in enumerate(missing):
                    out.append(RefWord(i, tokens[i].text, t0 + slot * n, t0 + slot * (n + 1)))
        start, end = spans[tok]
        out.append(RefWord(tok, tokens[tok].text, start, end))
    return out


def transcribe_words(model: Any, audio: np.ndarray, *, language: str = "es",
                     beam_size: int = REF_BEAM_SIZE) -> list[tuple[str, float, float]]:
    """Palabras con marcas de tiempo (texto, inicio s, fin s) del audio completo.

    `model` es un `faster_whisper.WhisperModel`. Usa el VAD de faster-whisper
    para no transcribir alucinaciones en los silencios largos (las marcas se
    devuelven en tiempos del audio original).
    """
    segments, _info = model.transcribe(
        np.ascontiguousarray(audio, dtype=np.float32), language=language,
        beam_size=beam_size, word_timestamps=True, condition_on_previous_text=False,
        vad_filter=True)
    return [(w.word, float(w.start), float(w.end))
            for seg in segments for w in (seg.words or [])]


def build_reference(audio: np.ndarray, tokens: Sequence[Token], model: Any, *,
                    language: str = "es", label: str = "") -> Reference:
    """Referencia offline: Whisper con marcas por palabra alineado con el guion."""
    words = transcribe_words(model, audio, language=language)
    ref = Reference(words=reference_from_words(words, tokens),
                    source=f"offline {label}".strip())
    log.info("Referencia offline: %d palabras transcritas, %d tokens con tiempo.",
             len(words), len(ref.words))
    return ref


# ---------------------------------------------------------------- simulación

def simulate(audio: np.ndarray, *, cycle: Any, matcher: Any, step_ms: float,
             window_s: float, sample_rate: int = SAMPLE_RATE,
             block_ms: float = DEFAULT_BLOCK_MS, prompt_context: bool = False,
             prompt_words: int = PROMPT_WORDS,
             timer: Callable[[], float] = time.perf_counter) -> list[TimelineRow]:
    """Corre el bucle de voz sobre `audio` con un reloj virtual.

    Replica `VoiceLoop._cycle`: prompt y texto cercano se toman del matcher al
    empezar el ciclo, `cycle.transcribe` corre (y se mide con `timer`, en
    segundos) y `cycle.apply` actualiza el matcher. El primer ciclo ocurre en
    t = step; el siguiente, en max(t + step, t + d).
    """
    if not step_ms > 0:
        raise ValueError("step_ms debe ser positivo")
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    n = audio.size
    duration_ms = n * 1000.0 / sample_rate
    win = max(1, int(round(window_s * sample_rate)))
    rows: list[TimelineRow] = []
    t = float(step_ms)
    while t <= duration_ms + 1e-6:
        avail_ms = math.floor(t / block_ms + 1e-9) * block_ms if block_ms > 0 else t
        end = min(n, int(round(avail_ms * sample_rate / 1000.0)))
        window = audio[max(0, end - win):end]
        prompt = matcher.context_before(prompt_words) if prompt_context else None
        near_text = matcher.text_near()
        t0 = timer()
        out = cycle.transcribe(window, prompt=prompt, near_text=near_text)
        d_ms = max(0.0, (timer() - t0) * 1000.0)
        before = matcher.current_idx
        match = cycle.apply(out, matcher)
        after = matcher.current_idx
        rows.append(TimelineRow(
            t_audio_ms=t, t_emit_ms=t + d_ms, voiced=bool(out.voiced),
            dropped=out.dropped, text=out.text or "", idx_before=before, idx_after=after,
            advanced=bool(match is not None and match.advanced),
            resync=bool(match is not None and getattr(match, "resync", False)),
            desync=bool(match is not None and getattr(match, "desync", False)),
            stt_ms=int(out.stt_ms or 0)))
        t = max(t + step_ms, t + d_ms)
    return rows


# ---------------------------------------------------------------- métricas

def _as_row(row: Any) -> TimelineRow:
    if isinstance(row, TimelineRow):
        return row
    return TimelineRow(**{k: row[k] for k in TIMELINE_FIELDS if k in row})


def _percentile(values: Sequence[float], q: float) -> float | None:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q)) if values else None


def compute_metrics(timeline: Sequence[Any], ref: Any, total_tokens: int, *,
                    false_jump_tokens: int = FALSE_JUMP_TOKENS,
                    skip_min_tokens: int = SKIP_MIN_TOKENS) -> dict[str, Any]:
    """Métricas de sincronización de una línea de tiempo contra la referencia.

    `timeline`: filas `TimelineRow` (o dicts con sus campos) en orden de
    tiempo. `ref`: `Reference`, dict con "words" o lista de palabras
    (`token_idx`, `start_ms`, `end_ms`) en orden de lectura; todas sus palabras
    son tokens con unidades. Ver el docstring del módulo.
    """
    rows = [_as_row(r) for r in timeline]
    words = [w for w in as_reference(ref).words if 0 <= w.token_idx < max(0, total_tokens)]

    # Primera vez que se dice cada token.
    first: dict[int, RefWord] = {}
    for w in words:
        first.setdefault(w.token_idx, w)
    spoken = sorted(first)

    # Primera fila en la que el índice pasó cada token: máximo acumulado.
    prefix_max: list[int] = []
    best = -1
    for r in rows:
        best = max(best, r.idx_after)
        prefix_max.append(best)
    delays: list[float] = []
    for tok in spoken:
        i = bisect.bisect_right(prefix_max, tok)
        if i < len(rows):
            delays.append(rows[i].t_emit_ms - first[tok].end_ms)
    passed = len(delays)

    # Posición de referencia en el instante t: 1 + mayor token ya empezado.
    by_start = sorted(words, key=lambda w: w.start_ms)
    starts = [w.start_ms for w in by_start]
    running: list[int] = []
    top = -1
    for w in by_start:
        top = max(top, w.token_idx)
        running.append(top)

    def ref_pos(t: float) -> int:
        k = bisect.bisect_right(starts, t)
        return running[k - 1] + 1 if k else 0

    emissions = [r for r in rows if r.idx_after != r.idx_before]
    overshoots = [r.idx_after - ref_pos(r.t_emit_ms) for r in emissions
                  if r.idx_after > r.idx_before or r.resync]
    false_jumps = sum(1 for o in overshoots if o > false_jump_tokens)
    regressions = [r for r in emissions if r.idx_after < r.idx_before]

    # Saltos hacia adelante de la referencia y tiempo de resincronización.
    resync_ms: list[float | None] = []
    for a, b in itertools.pairwise(words):
        if b.token_idx - a.token_idx > skip_min_tokens:
            hit = next((r for r in rows if r.idx_after >= b.token_idx), None)
            resync_ms.append(None if hit is None else hit.t_emit_ms - b.start_ms)
    resync_done = [v for v in resync_ms if v is not None]

    voiced = [r for r in rows if r.voiced]
    stt = [r.stt_ms for r in voiced if r.stt_ms > 0]
    proc = [r.t_emit_ms - r.t_audio_ms for r in voiced]
    return {
        "total_tokens": int(total_tokens),
        "spoken_tokens": len(spoken),
        "passed_tokens": passed,
        "never_passed": len(spoken) - passed,
        "coverage": passed / len(spoken) if spoken else None,
        "delay_p50_ms": _percentile(delays, 50),
        "delay_p95_ms": _percentile(delays, 95),
        "delay_max_ms": max(delays) if delays else None,
        "delay_mean_ms": statistics.fmean(delays) if delays else None,
        "false_jumps": false_jumps,
        "max_overshoot": max(overshoots) if overshoots else 0,
        "regressions": len(regressions),
        "regressions_resync": sum(1 for r in regressions if r.resync),
        "skips": len(resync_ms),
        "resync_ms": resync_ms,
        "resync_max_ms": max(resync_done) if resync_done else None,
        "resync_missed": sum(1 for v in resync_ms if v is None),
        "advances_total": sum(1 for r in rows if r.idx_after > r.idx_before),
        "emissions": len(emissions),
        "resyncs": sum(1 for r in rows if r.resync),
        "desync_cycles": sum(1 for r in rows if r.desync),
        "cycles": len(rows),
        "voiced_cycles": len(voiced),
        "dropped_cycles": sum(1 for r in voiced if r.dropped),
        "stt_ms_mean": statistics.fmean(stt) if stt else None,
        "stt_ms_max": max(stt) if stt else None,
        "proc_ms_mean": statistics.fmean(proc) if proc else None,
        "final_idx": rows[-1].idx_after if rows else 0,
    }


# ---------------------------------------------------------------- corrida

def load_stt(config: ReplayConfig) -> Any:
    """`STTEngine` real cargado (con warmup). Lanza `STTError` si falta el modelo."""
    from tools.stt_engine import STTEngine

    stt = STTEngine(config.model, config.device, config.compute, config.language)
    stt.load()
    if stt.fallback_reason:
        log.warning("STT en CPU: %s", stt.fallback_reason)
    return stt


def load_vad(config: ReplayConfig) -> Any:
    from tools.vad_gate import VadGate

    return VadGate(threshold=config.vad_threshold, sample_rate=SAMPLE_RATE)


def _load_audio(wav: str | os.PathLike | np.ndarray) -> np.ndarray:
    if isinstance(wav, np.ndarray):
        return np.ascontiguousarray(wav, dtype=np.float32).reshape(-1)
    from tools.audio_file_source import decode_file

    return decode_file(wav, SAMPLE_RATE)


def _load_script(script: str | os.PathLike | ParsedScript) -> ParsedScript:
    if isinstance(script, ParsedScript):
        return script
    return parse_script(Path(script).read_text(encoding="utf-8"))


def run_replay(wav: str | os.PathLike | np.ndarray, script: str | os.PathLike | ParsedScript, *,
               config: ReplayConfig | None = None, ref: Any = None, stt: Any = None,
               vad: Any = None, ref_model: Any = None, settings: Any = None,
               timer: Callable[[], float] = time.perf_counter) -> ReplayResult:
    """Corre el pipeline real sobre un WAV (o muestras a 16 kHz) y calcula las métricas.

    `stt` y `vad` se crean y cargan si no se pasan (permite reutilizarlos entre
    corridas o inyectar falsos). `ref`: ruta a un `.ref.json`, `Reference`,
    dict, o None para la referencia offline, que usa `ref_model` (un
    `WhisperModel`) o, si falta, el modelo de `stt`.
    """
    import backend.voice as voice
    from tools.script_matcher import ScriptMatcher

    if config is None:
        from backend.config import load_settings

        config = ReplayConfig.from_settings(load_settings())
    audio = _load_audio(wav)
    parsed = _load_script(script)
    if stt is None:
        stt = load_stt(config)
    if vad is None:
        vad = load_vad(config)
    if ref is None:
        model = ref_model if ref_model is not None else getattr(stt, "model", None)
        if model is None:
            raise ValueError("sin --ref hace falta un modelo de Whisper para la referencia")
        reference = build_reference(audio, parsed.tokens, model, language=config.language,
                                    label=getattr(stt, "model_size", config.model))
    elif isinstance(ref, (str, os.PathLike)):
        reference = load_reference(ref)
    else:
        reference = as_reference(ref)

    matcher = ScriptMatcher(parsed.tokens, **config.matcher_params)
    cycle = voice.VoiceCycle(stt, vad, config.to_settings(settings))
    pad = np.zeros(int(round(config.pad_s * SAMPLE_RATE)), dtype=np.float32)
    timeline = simulate(np.concatenate([audio, pad]), cycle=cycle, matcher=matcher,
                        step_ms=config.step_ms, window_s=config.window_s,
                        block_ms=config.block_ms, prompt_context=config.prompt_context,
                        prompt_words=getattr(voice, "PROMPT_WORDS", PROMPT_WORDS),
                        timer=timer)
    metrics = compute_metrics(timeline, reference, parsed.total_tokens)
    metrics["late_cycles"] = sum(1 for r in timeline
                                 if r.t_emit_ms - r.t_audio_ms > config.step_ms)
    name = "<muestras>" if isinstance(wav, np.ndarray) else str(wav)
    return ReplayResult(wav=name, config=config, timeline=timeline, reference=reference,
                        metrics=metrics, total_tokens=parsed.total_tokens,
                        duration_ms=audio.size * 1000.0 / SAMPLE_RATE)


def run_sweep(wavs: Sequence[tuple[str | os.PathLike, Any]],
              script: str | os.PathLike | ParsedScript, configs: Sequence[ReplayConfig], *,
              ref_config: ReplayConfig | None = None,
              progress: Callable[[str], None] | None = None) -> list[ReplayResult]:
    """Corre cada configuración sobre cada WAV y devuelve los resultados.

    `wavs` son pares (ruta, ref) con ref como en `run_replay`. Cada modelo se
    carga una sola vez, y la referencia offline de cada WAV se calcula una
    sola vez (con el modelo de `ref_config`, o el de la primera configuración)
    y se comparte entre configuraciones.
    """
    parsed = _load_script(script)
    stts: dict[tuple[str, str, str, str], Any] = {}
    vads: dict[float, Any] = {}

    def stt_for(cfg: ReplayConfig) -> Any:
        key = (cfg.model, cfg.device, cfg.compute, cfg.language)
        if key not in stts:
            if progress:
                progress(f"Cargando Whisper '{cfg.model}' ({cfg.device}/{cfg.compute})...")
            stts[key] = load_stt(cfg)
        return stts[key]

    results: list[ReplayResult] = []
    for wav, ref in wavs:
        audio = _load_audio(wav)
        if ref is None:
            base = ref_config or configs[0]
            if progress:
                progress(f"Referencia offline de {Path(wav).name} con '{base.model}'...")
            ref_stt = stt_for(base)
            reference: Any = build_reference(audio, parsed.tokens, ref_stt.model,
                                             language=base.language, label=base.model)
        elif isinstance(ref, (str, os.PathLike)):
            reference = load_reference(ref)
        else:
            reference = as_reference(ref)
        for cfg in configs:
            stt = stt_for(cfg)
            if cfg.vad_threshold not in vads:
                vads[cfg.vad_threshold] = load_vad(cfg)
            if progress:
                progress(f"Replay de {Path(wav).name} [{cfg.label()}]...")
            result = run_replay(audio, parsed, config=cfg, ref=reference, stt=stt,
                                vad=vads[cfg.vad_threshold])
            result.wav = str(wav)
            results.append(result)
    return results


# ---------------------------------------------------------------- salida

def _fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}"


def _fmt_resync(values: Sequence[float | None]) -> str:
    if not values:
        return "-"
    return " ".join("nunca" if v is None else f"{v / 1000:.1f}" for v in values)


def verdict(metrics: Mapping[str, Any]) -> list[str]:
    """Objetivos de la Fase 2 que no se cumplen (vacía si cumple todo lo aplicable)."""
    fails: list[str] = []
    if metrics.get("spoken_tokens"):
        p50, p95 = metrics.get("delay_p50_ms"), metrics.get("delay_p95_ms")
        if p50 is None or p50 > TARGET_P50_MS:
            fails.append(f"p50 > {TARGET_P50_MS} ms")
        if p95 is None or p95 > TARGET_P95_MS:
            fails.append(f"p95 > {TARGET_P95_MS} ms")
    elif metrics.get("advances_total"):
        fails.append("avances en silencio")
    if metrics.get("false_jumps"):
        fails.append("saltos falsos")
    if metrics.get("regressions", 0) - metrics.get("regressions_resync", 0) > 0:
        fails.append("retrocesos")
    resync = metrics.get("resync_ms") or []
    if any(v is None or v > TARGET_RESYNC_MS for v in resync):
        fails.append(f"resync > {TARGET_RESYNC_MS / 1000:g} s")
    return fails


_COLUMNS = (
    ("WAV", lambda r: Path(r.wav).stem),
    ("Config", lambda r: r.config.label()),
    ("p50", lambda r: _fmt_ms(r.metrics["delay_p50_ms"])),
    ("p95", lambda r: _fmt_ms(r.metrics["delay_p95_ms"])),
    ("máx", lambda r: _fmt_ms(r.metrics["delay_max_ms"])),
    ("nunca", lambda r: str(r.metrics["never_passed"])),
    ("falsos", lambda r: str(r.metrics["false_jumps"])),
    ("retroc", lambda r: f"{r.metrics['regressions']}"
     + (f" ({r.metrics['regressions_resync']}r)" if r.metrics["regressions_resync"] else "")),
    ("cobert", lambda r: "-" if r.metrics["coverage"] is None
     else f"{100 * r.metrics['coverage']:.0f}%"),
    ("resync s", lambda r: _fmt_resync(r.metrics["resync_ms"])),
    ("avances", lambda r: str(r.metrics["advances_total"])),
    ("desync", lambda r: str(r.metrics["desync_cycles"])),
    ("stt ms", lambda r: _fmt_ms(r.metrics["stt_ms_mean"])),
    ("ciclos v/t", lambda r: f"{r.metrics['voiced_cycles']}/{r.metrics['cycles']}"),
    ("final", lambda r: f"{r.metrics['final_idx']}/{r.total_tokens}"),
    ("veredicto", lambda r: "CUMPLE" if not verdict(r.metrics)
     else "NO: " + ", ".join(verdict(r.metrics))),
)


def format_table(results: Sequence[ReplayResult]) -> str:
    """Tabla de texto con una fila por corrida (también sirve de comparación)."""
    header = [name for name, _ in _COLUMNS]
    body = [[fn(r) for _, fn in _COLUMNS] for r in results]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(header, widths)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in body]
    return "\n".join(lines)


def format_summary(result: ReplayResult) -> str:
    """Resumen legible de una corrida (sin texto transcrito)."""
    m = result.metrics
    lines = [
        f"WAV: {result.wav} ({result.duration_ms / 1000:.1f} s)",
        f"Config: {result.config.label()}",
        f"Referencia: {result.reference.source or '-'} ({m['spoken_tokens']} tokens dichos "
        f"de {result.total_tokens})",
        f"Retraso: p50 {_fmt_ms(m['delay_p50_ms'])} ms, p95 {_fmt_ms(m['delay_p95_ms'])} ms, "
        f"máx {_fmt_ms(m['delay_max_ms'])} ms; nunca pasados {m['never_passed']}",
        f"Saltos falsos {m['false_jumps']} (mayor adelanto {m['max_overshoot']}), "
        f"retrocesos {m['regressions']} (por resync {m['regressions_resync']}), "
        f"cobertura " + ("-" if m["coverage"] is None else f"{100 * m['coverage']:.1f}%"),
        f"Saltos de párrafo {m['skips']}: resync {_fmt_resync(m['resync_ms'])} s; "
        f"avances {m['advances_total']}, resyncs {m['resyncs']}, ciclos desync "
        f"{m['desync_cycles']}",
        f"Ciclos {m['cycles']} (con voz {m['voiced_cycles']}, filtrados "
        f"{m['dropped_cycles']}, atrasados {m.get('late_cycles', 0)}); stt medio "
        f"{_fmt_ms(m['stt_ms_mean'])} ms, máx {_fmt_ms(m['stt_ms_max'])} ms; índice final "
        f"{m['final_idx']}/{result.total_tokens}",
    ]
    fails = verdict(m)
    lines.append("Objetivos Fase 2: " + ("CUMPLE" if not fails else "NO CUMPLE (" +
                                         ", ".join(fails) + ")"))
    return "\n".join(lines)


def write_csv(path: str | os.PathLike, timeline: Sequence[TimelineRow]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(TIMELINE_FIELDS)
        for r in timeline:
            row = dataclasses.asdict(r)
            row["t_audio_ms"] = f"{r.t_audio_ms:.1f}"
            row["t_emit_ms"] = f"{r.t_emit_ms:.1f}"
            row["dropped"] = r.dropped or ""
            writer.writerow([row[k] for k in TIMELINE_FIELDS])


def result_to_json(result: ReplayResult, script: str | None = None) -> dict:
    return {
        "wav": result.wav, "script": script, "duration_ms": round(result.duration_ms),
        "total_tokens": result.total_tokens, "config": dataclasses.asdict(result.config),
        "metrics": result.metrics, "reference": result.reference.to_json(script),
        "timeline": [dataclasses.asdict(r) for r in result.timeline],
    }


# ---------------------------------------------------------------- CLI

def _parse_value(key: str, raw: str) -> Any:
    if key in ("model", "device", "compute", "language"):
        return raw
    if key == "prompt_context":
        text = raw.strip().lower()
        if text in ("1", "true", "si", "sí", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"prompt_context: valor no válido {raw!r}")
    if key in ("step_ms", "block_ms"):
        return int(raw)
    return float(raw)


def expand_sweep(base: ReplayConfig, items: Sequence[str]) -> list[ReplayConfig]:
    """Configuraciones del producto cartesiano de `clave=v1,v2,...`.

    Las claves son campos de `ReplayConfig` (step_ms, window_s,
    prompt_context, model...) o parámetros del matcher (min_score...).
    """
    axes: list[tuple[str, list[Any]]] = []
    for item in items:
        key, sep, values = item.partition("=")
        key = key.strip()
        if not sep or not key or not values.strip():
            raise ValueError(f"--sweep espera clave=v1,v2 (recibido {item!r})")
        axes.append((key, [_parse_value(key, v.strip()) for v in values.split(",") if v.strip()]))
    configs: list[ReplayConfig] = []
    for combo in itertools.product(*(vals for _key, vals in axes)):
        cfg = dataclasses.replace(base, matcher_params=dict(base.matcher_params))
        for (key, _vals), value in zip(axes, combo):
            if key in _CONFIG_KEYS:
                cfg = dataclasses.replace(cfg, **{key: value})
            else:
                cfg.matcher_params[key] = value
        configs.append(cfg)
    return configs


def _resolve(path: str | os.PathLike) -> Path:
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    alt = ROOT / p
    return alt if alt.exists() else p


def _out_path(base: str, result: ReplayResult, ext: str, many: bool) -> Path:
    if not many:
        return Path(base)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in result.config.label())
    return Path(base) / f"{Path(result.wav).stem}__{safe}.{ext}"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", nargs="+", required=True, help="uno o más WAV")
    ap.add_argument("--script", help="guion (si falta, el que indica el .ref.json)")
    ap.add_argument("--ref", help="referencia .ref.json, o 'auto' para <wav>.ref.json")
    ap.add_argument("--save-ref", help="guarda la referencia usada (un solo WAV)")
    ap.add_argument("--ref-model", help="modelo de la referencia offline (por defecto el del replay)")
    ap.add_argument("--step-ms", type=int, help="periodo del bucle (TP_STEP_MS)")
    ap.add_argument("--window-s", type=float, help="ventana de audio (TP_WINDOW_S)")
    ap.add_argument("--prompt-context", action=argparse.BooleanOptionalAction, default=None,
                    help="guion previo como initial_prompt (TP_PROMPT_CONTEXT)")
    ap.add_argument("--model", help="modelo de Whisper (TP_MODEL_SIZE)")
    ap.add_argument("--device", choices=("cuda", "cpu"), help="TP_DEVICE")
    ap.add_argument("--compute", help="tipo de cómputo (TP_COMPUTE)")
    ap.add_argument("--vad-threshold", type=float, help="umbral de Silero (TP_VAD_THRESHOLD)")
    ap.add_argument("--param", action="append", default=[], metavar="CLAVE=VALOR",
                    help="parámetro del matcher (repetible), por ejemplo min_score=3.5")
    ap.add_argument("--pad-s", type=float, default=DEFAULT_PAD_S,
                    help=f"silencio tras el WAV ({DEFAULT_PAD_S:g} s por defecto)")
    ap.add_argument("--block-ms", type=int, default=DEFAULT_BLOCK_MS,
                    help="bloque de la captura en vivo (100 por defecto; 0 sin bloques)")
    ap.add_argument("--sweep", nargs="+", metavar="CLAVE=V1,V2",
                    help="barre configuraciones: step_ms, window_s, prompt_context, model, "
                         "vad_threshold o parámetros del matcher")
    ap.add_argument("--csv", help="línea de tiempo en CSV (con varias corridas: carpeta)")
    ap.add_argument("--json", help="resultado completo en JSON (con varias corridas: carpeta)")
    ap.add_argument("-v", "--verbose", action="store_true", help="registro detallado")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    from backend.config import load_settings

    wavs = [_resolve(w) for w in args.wav]
    for w in wavs:
        if not w.is_file():
            ap.error(f"no existe el WAV {w}")
    refs: list[Path | None] = []
    for w in wavs:
        if args.ref == "auto":
            cand = w.with_name(w.stem + ".ref.json")
            refs.append(cand if cand.is_file() else None)
        elif args.ref:
            if len(wavs) > 1:
                ap.error("con varios WAV, --ref solo admite 'auto'")
            ref_path = _resolve(args.ref)
            if not ref_path.is_file():
                ap.error(f"no existe la referencia {ref_path}")
            refs.append(ref_path)
        else:
            refs.append(None)
    if args.save_ref and len(wavs) > 1:
        ap.error("--save-ref necesita un solo WAV")

    script_arg = args.script
    if script_arg is None:
        for r in refs:
            if r is not None:
                script_arg = json.loads(r.read_text(encoding="utf-8-sig")).get("script")
                if script_arg:
                    break
    if not script_arg:
        ap.error("falta --script")
    script_path = _resolve(script_arg)
    if not script_path.is_file():
        ap.error(f"no existe el guion {script_path}")

    try:
        params = {}
        for item in args.param:
            key, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"--param espera clave=valor (recibido {item!r})")
            params[key.strip()] = float(value)
        base = ReplayConfig.from_settings(
            load_settings(), step_ms=args.step_ms, window_s=args.window_s,
            prompt_context=args.prompt_context, model=args.model, device=args.device,
            compute=args.compute, vad_threshold=args.vad_threshold, pad_s=args.pad_s,
            block_ms=args.block_ms, matcher_params=params)
        configs = expand_sweep(base, args.sweep) if args.sweep else [base]
        from tools.script_matcher import ScriptMatcher

        for cfg in configs:  # parámetros del matcher desconocidos: error antes de cargar nada
            ScriptMatcher(**cfg.matcher_params)
    except (ValueError, TypeError) as exc:
        ap.error(str(exc))
    ref_config = dataclasses.replace(configs[0], model=args.ref_model) if args.ref_model else None

    try:
        results = run_sweep(list(zip(wavs, refs)), script_path, configs,
                            ref_config=ref_config, progress=lambda s: print(f"[i] {s}", flush=True))
    except Exception as exc:  # STTError (modelo ausente), audio ilegible...
        code = getattr(exc, "code", None)
        print(f"[X] {'(' + code + ') ' if code else ''}{exc}")
        if args.verbose:
            log.exception("Detalle del error")
        return 1

    print()
    for result in results:
        print(format_summary(result))
        print()
    print(format_table(results))

    many = len(results) > 1
    shown_script = str(script_arg)
    for result in results:
        if args.csv:
            path = _out_path(args.csv, result, "csv", many)
            write_csv(path, result.timeline)
            print(f"[OK] CSV: {path}")
        if args.json:
            path = _out_path(args.json, result, "json", many)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result_to_json(result, shown_script), ensure_ascii=False,
                                       indent=1), encoding="utf-8")
            print(f"[OK] JSON: {path}")
    if args.save_ref:
        Path(args.save_ref).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save_ref).write_text(
            json.dumps(results[0].reference.to_json(shown_script), ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"[OK] Referencia: {args.save_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
