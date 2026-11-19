"""Genera grabaciones sintéticas en español de un guion con la voz TTS de Windows.

Uso:
  .venv312\\Scripts\\python.exe tools\\synth_sample.py --scenario normal
  .venv312\\Scripts\\python.exe tools\\synth_sample.py --scenario salto --skip-paragraph 2
  .venv312\\Scripts\\python.exe tools\\synth_sample.py --scenario relectura --rate 2 --noise-db -45
  .venv312\\Scripts\\python.exe tools\\synth_sample.py --scenario silencio --seconds 15

Escenarios:
  normal           lee todas las palabras del guion en orden.
  salto            se salta un párrafo entero (el 2.º por defecto, `--skip-paragraph`).
  relectura        tras una frase cercana a la mitad relee sus últimas 6 a 8 palabras
                   (`--reread-words`, 7 por defecto) y sigue.
  salto_relectura  las dos cosas.
  silencio         sin voz: `--seconds` de ruido de fondo (unos -50 dBFS) con algunos clics.

Salida en `--out-dir` (por defecto tests/fixtures/audio_synth, ignorada por git):
  <nombre>.wav       16 kHz, 16 bits, mono.
  <nombre>.ref.json  {"script", "sample_rate", "words": [{"token_idx", "text",
                     "start_ms", "end_ms"}], "events": [...]} donde `token_idx` es el
                     token de `parse_script` que se dijo y `events` lista los saltos,
                     las relecturas (y los clics del silencio) con sus tiempos.

La voz es "Microsoft Sabina Desktop" (System.Speech desde Windows PowerShell). El
texto viaja en un archivo temporal UTF-8, nunca en la línea de comandos. El inicio
de cada palabra sale del evento SpeakProgress (AudioPosition y CharacterPosition);
el fin es el inicio de la palabra siguiente, recortado al último tramo con voz del
audio limpio (así la pausa tras un punto no cuenta como parte de la palabra).
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import subprocess
import sys
import tempfile
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # ejecutado como script: `tools\synth_sample.py`
    sys.path.insert(0, str(ROOT))

from tools.script_parser import ParsedScript, parse_script  # noqa: E402

SAMPLE_RATE = 16000
DEFAULT_SCRIPT = ROOT / "guiones" / "ejemplo.txt"
DEFAULT_OUT_DIR = ROOT / "tests" / "fixtures" / "audio_synth"
DEFAULT_VOICE = "Microsoft Sabina Desktop"
SCENARIOS = ("normal", "salto", "relectura", "salto_relectura", "silencio")

LAST_WORD_MS = 400        # duración supuesta de la última palabra (sin palabra siguiente)
REREAD_MIN, REREAD_MAX = 6, 8
DEFAULT_REREAD = 7
DEFAULT_SKIP_PARAGRAPH = 2
SILENCE_DB = -50.0        # nivel RMS del ruido del escenario silencio
CLICK_DB = -20.0          # pico de cada clic
CLICK_MS = 4.0            # duración de cada clic
VOICED_DB = -50.0         # umbral de voz para recortar el fin de cada palabra
FRAME_MS = 10.0           # tramo de análisis del recorte
MIN_WORD_MS = 40          # una palabra dura al menos esto tras el recorte
MIN_GAP_MS = 80.0         # pausa mínima que separa una palabra de la siguiente
TTS_TIMEOUT_S = 600.0

# Signos que cierran una frase (tras quitar comillas y paréntesis de cierre).
_SENTENCE_END = (".", "!", "?", "…")
_CLOSERS = "\"'»”’)]}"

# Script de Windows PowerShell 5.1 (System.Speech no existe en PowerShell 7).
# Los eventos SpeakProgress se atienden de forma asíncrona: se espera hasta que
# la lista deja de crecer antes de leerla.
_PS_SCRIPT = r"""
param([string]$In, [string]$Wav, [string]$Json, [int]$Rate = 0,
      [string]$Voice = "Microsoft Sabina Desktop")
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $s.SelectVoice($Voice)
    $s.Rate = $Rate
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $text = [System.IO.File]::ReadAllText($In, [System.Text.Encoding]::UTF8)
    $Global:wl = New-Object System.Collections.ArrayList
    Register-ObjectEvent -InputObject $s -EventName SpeakProgress -SourceIdentifier sp -Action {
        [void]$Global:wl.Add(@{ t = $EventArgs.AudioPosition.TotalMilliseconds;
            pos = $EventArgs.CharacterPosition; len = $EventArgs.CharacterCount;
            text = $EventArgs.Text })
    } | Out-Null
    $s.SetOutputToWaveFile($Wav, $fmt)
    $s.Speak($text)
    $s.SetOutputToNull()
    $prev = -1
    while ($Global:wl.Count -ne $prev) {
        $prev = $Global:wl.Count
        Start-Sleep -Milliseconds 300
    }
    Unregister-Event -SourceIdentifier sp
    ConvertTo-Json -InputObject @($Global:wl) -Depth 3 | Out-File -Encoding utf8 $Json
} finally {
    $s.Dispose()
}
"""


# ---------------------------------------------------------------- plan de lectura

@dataclass
class Plan:
    """Qué tokens se dicen y en qué orden.

    `order` son índices de token (puede repetir tokens en una relectura);
    `para_before` son las posiciones de `order` precedidas por una pausa de
    párrafo; cada evento lleva `pos`, la posición de `order` donde empieza.
    """

    order: list[int] = field(default_factory=list)
    para_before: set[int] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)


class Span(NamedTuple):
    """Tramo de una palabra dicha en el texto del TTS, en unidades UTF-16."""

    start: int
    end: int
    token_idx: int
    text: str


def paragraphs(parsed: ParsedScript) -> list[list[int]]:
    """Índices de los tokens con unidades de cada párrafo (separados por `break` paragraph)."""
    paras: list[list[int]] = [[]]
    for seg in parsed.segments:
        if seg["type"] == "break" and seg.get("kind") == "paragraph":
            if paras[-1]:
                paras.append([])
        elif seg["type"] == "word" and parsed.tokens[seg["idx"]].units:
            paras[-1].append(seg["idx"])
    return [p for p in paras if p]


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_SENTENCE_END)


def sentences(parsed: ParsedScript, paras: list[list[int]] | None = None) -> list[list[int]]:
    """Frases (listas de índices de token); el fin de párrafo también cierra la frase."""
    out: list[list[int]] = []
    for para in paragraphs(parsed) if paras is None else paras:
        cur: list[int] = []
        for idx in para:
            cur.append(idx)
            if _ends_sentence(parsed.tokens[idx].text):
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
    return out


def build_plan(parsed: ParsedScript, scenario: str, *,
               skip_paragraph: int = DEFAULT_SKIP_PARAGRAPH,
               reread_words: int = DEFAULT_REREAD) -> Plan:
    """Orden de lectura del escenario (todos menos `silencio`).

    Lanza `ValueError` si el guion no permite el escenario (por ejemplo, un
    salto sin párrafos antes y después del saltado).
    """
    if scenario not in SCENARIOS or scenario == "silencio":
        raise ValueError(f"escenario sin lectura: {scenario!r}")
    paras = paragraphs(parsed)
    if not paras:
        raise ValueError("el guion no tiene palabras que leer")
    do_skip = scenario in ("salto", "salto_relectura")
    do_reread = scenario in ("relectura", "salto_relectura")

    skipped: int | None = None
    if do_skip:
        if not 2 <= skip_paragraph <= len(paras) - 1:
            raise ValueError(
                f"--skip-paragraph debe estar entre 2 y {len(paras) - 1} (el párrafo saltado "
                f"necesita párrafos antes y después; el guion tiene {len(paras)})")
        skipped = skip_paragraph - 1

    reread_sentence: list[int] | None = None
    reread_n = 0
    if do_reread:
        if not REREAD_MIN <= reread_words <= REREAD_MAX:
            raise ValueError(f"--reread-words debe estar entre {REREAD_MIN} y {REREAD_MAX}")
        kept = [p for i, p in enumerate(paras) if i != skipped]
        sents = sentences(parsed, kept)[:-1]  # tras la relectura hay que seguir leyendo
        candidates = [s for s in sents if len(s) >= REREAD_MIN]
        if not candidates:
            raise ValueError(f"no hay ninguna frase de {REREAD_MIN} palabras o más seguida "
                             "de otra para releer")
        content = [i for p in kept for i in p]
        middle = content[len(content) // 2]
        reread_sentence = min(candidates, key=lambda s: (abs(s[-1] - middle), s[-1]))
        reread_n = min(reread_words, len(reread_sentence))

    plan = Plan()
    for p_i, para in enumerate(paras):
        if p_i == skipped:
            continue
        if plan.order:
            plan.para_before.add(len(plan.order))
        if skipped is not None and p_i == skipped + 1:
            plan.events.append({
                "type": "skip", "paragraph": skip_paragraph,
                "skipped_from": paras[skipped][0], "skipped_to": paras[skipped][-1],
                "from_token": paras[skipped - 1][-1], "to_token": para[0],
                "pos": len(plan.order),
            })
        for idx in para:
            plan.order.append(idx)
            if reread_sentence is not None and idx == reread_sentence[-1]:
                again = reread_sentence[-reread_n:]
                plan.events.append({
                    "type": "reread", "sentence_end": idx, "from_token": again[0],
                    "to_token": again[-1], "words": len(again), "pos": len(plan.order),
                })
                plan.order.extend(again)
    return plan


def _u16len(s: str) -> int:
    """Largo en unidades UTF-16 (las posiciones de .NET cuentan así)."""
    return len(s.encode("utf-16-le")) // 2


def render_text(plan: Plan, parsed: ParsedScript) -> tuple[str, list[Span]]:
    """Texto que lee el TTS y el tramo (UTF-16) de cada palabra de `plan.order`."""
    parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    for i, idx in enumerate(plan.order):
        if i:
            sep = "\n\n" if i in plan.para_before else " "
            parts.append(sep)
            pos += _u16len(sep)
        text = parsed.tokens[idx].text
        n = _u16len(text)
        spans.append(Span(pos, pos + n, idx, text))
        parts.append(text)
        pos += n
    return "".join(parts), spans


# ---------------------------------------------------------------- tiempos de palabra

def map_progress_to_words(spans: Sequence[Span],
                          progress: Sequence[Mapping]) -> tuple[list[dict], int]:
    """Asigna cada evento SpeakProgress a la palabra dicha cuyo tramo lo contiene.

    `progress` son los eventos (`t` en ms, `pos` y `len` en unidades UTF-16).
    Un evento que cae en un separador se asigna a la palabra siguiente si su
    tramo la toca. Varios eventos de la misma palabra (por ejemplo "RTX-5070")
    se funden: el inicio es el del primero. Devuelve las palabras con evento,
    en orden de lectura, como dicts con `pos` (índice en `spans`), `token_idx`,
    `text`, `start_ms` y `end_ms` (inicio de la siguiente palabra, o el inicio
    más `LAST_WORD_MS` para la última), y la cantidad de eventos sin palabra.
    """
    starts = [s.start for s in spans]
    first_ms: dict[int, float] = {}
    unmapped = 0
    for ev in sorted(progress, key=lambda e: (float(e["t"]), int(e["pos"]))):
        pos = int(ev["pos"])
        length = max(1, int(ev.get("len") or 0))
        i = bisect.bisect_right(starts, pos) - 1
        if i >= 0 and pos < spans[i].end:
            hit = i
        elif i + 1 < len(spans) and pos + length > spans[i + 1].start:
            hit = i + 1
        else:
            unmapped += 1
            continue
        first_ms.setdefault(hit, float(ev["t"]))
    hits = sorted(first_ms)
    words: list[dict] = []
    for k, i in enumerate(hits):
        start = int(round(first_ms[i]))
        if k + 1 < len(hits):
            end = max(start, int(round(first_ms[hits[k + 1]])))
        else:
            end = start + LAST_WORD_MS
        words.append({"pos": i, "token_idx": spans[i].token_idx, "text": spans[i].text,
                      "start_ms": start, "end_ms": end})
    return words, unmapped


def trim_word_ends(words: list[dict], samples: np.ndarray, sample_rate: int = SAMPLE_RATE, *,
                   threshold_db: float = VOICED_DB, frame_ms: float = FRAME_MS,
                   min_gap_ms: float = MIN_GAP_MS) -> list[dict]:
    """Recorta `end_ms` de cada palabra al inicio de la pausa que la sigue.

    Entre `start_ms` y `end_ms` (el inicio de la palabra siguiente; para la
    última palabra, el fin del audio) busca el último tramo sin voz (nivel RMS
    por tramos de `frame_ms` bajo `threshold_db`) de al menos `min_gap_ms`, y
    pone el fin donde empieza. Se busca una pausa y no la última muestra con
    voz porque el audio de la palabra siguiente suele empezar unos ms antes
    de su evento SpeakProgress. Sin pausa (habla continua) el fin no cambia,
    salvo la última palabra, que termina con el audio. El fin nunca queda a
    menos de `MIN_WORD_MS` del inicio.
    """
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    frame = max(1, int(round(frame_ms * sample_rate / 1000)))
    n_frames = audio.size // frame
    if n_frames == 0:
        return [dict(w) for w in words]
    framed = audio[:n_frames * frame].reshape(n_frames, frame).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(framed), axis=1))
    silent = rms <= 10.0 ** (threshold_db / 20.0)
    min_gap = max(1, int(round(min_gap_ms / frame_ms)))
    frame_to_ms = frame * 1000.0 / sample_rate
    out: list[dict] = []
    for k, w in enumerate(words):
        w = dict(w)
        start = int(w["start_ms"])
        last = k == len(words) - 1
        limit_ms = audio.size * 1000.0 / sample_rate if last else float(w["end_ms"])
        f0 = int(start / frame_to_ms) + 1  # el primer tramo puede ser silencio previo
        f1 = min(n_frames, int(np.ceil(limit_ms / frame_to_ms)))
        gap_start = None
        run = 0
        for f in range(f1 - 1, f0 - 1, -1):  # de atrás hacia adelante: la última pausa
            if silent[f]:
                run += 1
                if run >= min_gap and (f == f0 or not silent[f - 1]):
                    gap_start = f
                    break
            else:
                if run >= min_gap:
                    gap_start = f + 1
                    break
                run = 0
        if gap_start is not None:
            w["end_ms"] = max(start + MIN_WORD_MS, int(round(gap_start * frame_to_ms)))
        elif last:
            w["end_ms"] = max(start + MIN_WORD_MS, int(round(limit_ms)))
        out.append(w)
    return out


# ---------------------------------------------------------------- audio

def read_wav(path: str | os.PathLike) -> tuple[np.ndarray, int]:
    """Muestras float32 mono y tasa de un WAV PCM de 16 bits."""
    with wave.open(os.fspath(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: se esperaba PCM de 16 bits")
        channels, rate = wf.getnchannels(), wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    samples = data.astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def write_wav(path: str | os.PathLike, samples: np.ndarray,
              sample_rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(os.fspath(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def noise(n: int, level_db: float, rng: np.random.Generator) -> np.ndarray:
    """Ruido blanco gaussiano con nivel RMS `level_db` dBFS."""
    return (rng.standard_normal(n) * 10.0 ** (level_db / 20.0)).astype(np.float32)


def make_silence(seconds: float, *, level_db: float = SILENCE_DB, clicks: int = 4,
                 seed: int = 1234,
                 sample_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, list[dict]]:
    """Ruido de fondo con algunos clics cortos; devuelve el audio y los clics (`t_ms`)."""
    rng = np.random.default_rng(seed)
    n = int(round(seconds * sample_rate))
    audio = noise(n, level_db, rng)
    events: list[dict] = []
    length = max(1, int(round(CLICK_MS * sample_rate / 1000)))
    margin = int(0.5 * sample_rate)
    if clicks > 0 and n > 2 * margin + length:
        positions = np.sort(rng.integers(margin, n - margin - length, size=clicks))
        envelope = np.exp(-np.arange(length) / (length / 4.0))
        peak = 10.0 ** (CLICK_DB / 20.0)
        for p in positions:
            burst = rng.uniform(-1.0, 1.0, length) * envelope * peak
            audio[p:p + length] += burst.astype(np.float32)
            events.append({"type": "click", "t_ms": int(round(p * 1000 / sample_rate))})
    return audio, events


# ---------------------------------------------------------------- TTS

def run_tts(text: str, wav_path: str | os.PathLike, *, voice: str = DEFAULT_VOICE,
            rate: int = 0, timeout: float = TTS_TIMEOUT_S) -> list[dict]:
    """Sintetiza `text` a `wav_path` (16 kHz, 16 bits, mono) y devuelve los SpeakProgress.

    Corre Windows PowerShell con System.Speech. Lanza `RuntimeError` si falla
    (por ejemplo, si la voz no está instalada).
    """
    if os.name != "nt":
        raise RuntimeError("la síntesis usa System.Speech y solo funciona en Windows")
    with tempfile.TemporaryDirectory(prefix="tp_synth_") as tmp:
        tmp_dir = Path(tmp)
        txt = tmp_dir / "texto.txt"
        ps1 = tmp_dir / "tts.ps1"
        out_json = tmp_dir / "progress.json"
        txt.write_bytes(text.encode("utf-8"))  # sin traducir saltos: las posiciones cuentan
        # Con BOM: Windows PowerShell 5.1 lee los .ps1 sin BOM como ANSI.
        ps1.write_text(_PS_SCRIPT, encoding="utf-8-sig")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(ps1), "-In", str(txt), "-Wav", os.fspath(wav_path),
               "-Json", str(out_json), "-Rate", str(int(rate)), "-Voice", voice]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise RuntimeError("no se encontró powershell.exe") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"la síntesis superó {timeout:.0f} s") from exc
        if proc.returncode != 0 or not out_json.is_file():
            err = proc.stderr.decode("utf-8", "replace") or proc.stdout.decode("utf-8", "replace")
            raise RuntimeError(f"falló la síntesis con PowerShell: {err.strip()[:800]}")
        raw = out_json.read_text(encoding="utf-8-sig").strip()
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):  # un solo evento
        data = [data]
    return [dict(ev) for ev in data]


# ---------------------------------------------------------------- composición

def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def default_name(script: Path, scenario: str, rate: int, noise_db: float | None) -> str:
    name = f"{script.stem}_{scenario}"
    if rate:
        name += f"_rate{rate:+d}"
    if noise_db is not None:
        name += f"_ruido{abs(noise_db):g}"
    return name


def synthesize(script: Path, scenario: str, out_dir: Path, name: str, *,
               rate: int = 0, noise_db: float | None = None, seconds: float = 15.0,
               skip_paragraph: int = DEFAULT_SKIP_PARAGRAPH, reread_words: int = DEFAULT_REREAD,
               voice: str = DEFAULT_VOICE, clicks: int = 4, seed: int = 1234) -> dict:
    """Genera `<name>.wav` y `<name>.ref.json` en `out_dir`; devuelve la referencia."""
    parsed = parse_script(script.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{name}.wav"
    ref_path = out_dir / f"{name}.ref.json"
    rng = np.random.default_rng(seed)
    ref: dict = {
        "script": _display_path(script), "scenario": scenario, "sample_rate": SAMPLE_RATE,
        "total_tokens": parsed.total_tokens,
    }
    if scenario == "silencio":
        audio, events = make_silence(seconds, clicks=clicks, seed=seed)
        if noise_db is not None:
            audio = audio + noise(audio.size, noise_db, rng)
        ref.update({"noise_db": noise_db, "seed": seed, "words": [], "events": events})
    else:
        plan = build_plan(parsed, scenario, skip_paragraph=skip_paragraph,
                          reread_words=reread_words)
        text, spans = render_text(plan, parsed)
        with tempfile.TemporaryDirectory(prefix="tp_synth_wav_") as tmp:
            tmp_wav = Path(tmp) / "tts.wav"
            progress = run_tts(text, tmp_wav, voice=voice, rate=rate)
            audio, sr = read_wav(tmp_wav)
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"el TTS devolvió {sr} Hz en lugar de {SAMPLE_RATE}")
        words, unmapped = map_progress_to_words(spans, progress)
        words = trim_word_ends(words, audio, SAMPLE_RATE)  # sobre el audio limpio
        by_pos = {w["pos"]: w for w in words}
        events = []
        for ev in plan.events:
            ev = dict(ev)
            pos = ev.pop("pos")
            first = by_pos.get(pos)
            ev["t_ms"] = first["start_ms"] if first else None
            if ev["type"] == "reread":
                last = by_pos.get(pos + ev["words"] - 1)
                ev["end_ms"] = last["end_ms"] if last else None
            events.append(ev)
        if noise_db is not None:
            audio = audio + noise(audio.size, noise_db, rng)
        ref.update({
            "voice": voice, "rate": rate, "noise_db": noise_db, "seed": seed,
            "spoken_words": len(spans), "unmapped_events": unmapped,
            "words": [{k: w[k] for k in ("token_idx", "text", "start_ms", "end_ms")}
                      for w in words],
            "events": events,
        })
    ref["duration_ms"] = int(round(audio.size * 1000 / SAMPLE_RATE))
    write_wav(wav_path, audio)
    ref_path.write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    ref["_wav"], ref["_ref"] = str(wav_path), str(ref_path)
    return ref


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=SCENARIOS, default="normal")
    ap.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                    help="guion a leer (guiones/ejemplo.txt por defecto)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--name", help="nombre sin extensión (por defecto <guion>_<escenario>)")
    ap.add_argument("--rate", type=int, default=0, choices=range(-10, 11), metavar="-10..10",
                    help="velocidad del TTS (0 por defecto)")
    ap.add_argument("--noise-db", type=float, default=None,
                    help="mezcla ruido blanco con este nivel RMS en dBFS (por ejemplo -45)")
    ap.add_argument("--seconds", type=float, default=15.0, help="duración del escenario silencio")
    ap.add_argument("--clicks", type=int, default=4, help="clics del escenario silencio")
    ap.add_argument("--skip-paragraph", type=int, default=DEFAULT_SKIP_PARAGRAPH,
                    help="párrafo que se salta (1 es el primero; 2 por defecto)")
    ap.add_argument("--reread-words", type=int, default=DEFAULT_REREAD,
                    help=f"palabras que se releen ({REREAD_MIN} a {REREAD_MAX})")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--seed", type=int, default=1234, help="semilla del ruido")
    args = ap.parse_args(argv)

    if not args.script.is_file():
        ap.error(f"no existe el guion {args.script}")
    if args.scenario == "silencio" and not args.seconds > 0:
        ap.error("--seconds debe ser positivo")
    name = args.name or default_name(args.script, args.scenario, args.rate, args.noise_db)
    try:
        ref = synthesize(args.script, args.scenario, args.out_dir, name, rate=args.rate,
                         noise_db=args.noise_db, seconds=args.seconds,
                         skip_paragraph=args.skip_paragraph, reread_words=args.reread_words,
                         voice=args.voice, clicks=args.clicks, seed=args.seed)
    except (ValueError, RuntimeError) as exc:
        print(f"[X] {exc}")
        return 1
    summary = f"{ref['duration_ms'] / 1000:.1f} s, {len(ref['words'])} palabras"
    if ref.get("unmapped_events"):
        summary += f", {ref['unmapped_events']} eventos sin palabra"
    if ref.get("spoken_words") and ref["spoken_words"] != len(ref["words"]):
        summary += f", {ref['spoken_words'] - len(ref['words'])} palabras sin evento"
    print(f"[OK] {ref['_wav']} ({summary})")
    for ev in ref["events"]:
        detail = ", ".join(f"{k}={v}" for k, v in ev.items() if k != "type")
        print(f"     {ev['type']}: {detail}")
    print(f"[OK] {ref['_ref']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
