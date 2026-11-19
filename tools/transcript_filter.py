"""Filtro de alucinaciones de Whisper: descarta segmentos y transcripciones que no son voz.

Whisper "inventa" texto ante el silencio o el ruido: créditos de subtítulos,
despedidas de video o anotaciones como "[Música]". Si ese texto llegara al
matcher podría hacer avanzar el guion cerca de un cierre que diga "muchas
gracias" y además contaría como un ciclo con voz sin coincidencia, lo que
empuja hacia una desincronización falsa (docs/diseno_v2.md, sección 5).

Tres capas, en este orden:

1. Segmentos: se descarta el que tiene `no_speech_prob` alto y
   `avg_logprob` bajo a la vez (Whisper cree que no hubo voz y además duda
   del texto) o un `compression_ratio` alto (texto repetitivo en bucle). Son
   los mismos criterios y umbrales que usa faster-whisper.
2. Lista negra: si lo que queda consiste solo en frases de `BLACKLIST_ES`
   (repetidas o combinadas, unidas a lo sumo por stopwords sueltas), se
   descarta, salvo que esa frase aparezca tal cual en el guion cercano
   (`near_text`): entonces el orador pudo decirla de verdad.
3. Una sola stopword ("y", "de"...) no aporta nada al matcher: se descarta.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tools.text_norm import is_stopword, to_units

# Motivos de descarte (`FilterOutcome.reason`).
REASON_EMPTY = "empty"              # Whisper no devolvió texto
REASON_NO_SPEECH = "no_speech"      # segmentos sin voz probable y con baja confianza
REASON_COMPRESSION = "compression"  # segmentos repetitivos (bucle de Whisper)
REASON_BLACKLIST = "blacklist"      # solo frases típicas de alucinación
REASON_STOPWORD = "stopword"        # una sola palabra funcional

# Frases que Whisper produce ante silencio o ruido en español. Vienen de los
# subtítulos de videos con los que se entrenó: créditos, despedidas,
# llamadas a suscribirse y anotaciones de sonido. Se escriben como texto
# normal y se normalizan con `to_units`, igual que la transcripción.
# Es la única lista: para agregar una frase basta con sumarla aquí.
_HALLUCINATED_PHRASES_ES: tuple[str, ...] = (
    # Créditos de subtítulos (el caso más frecuente)
    "Subtítulos realizados por la comunidad de Amara.org",
    "Subtitulado por la comunidad de Amara.org",
    "Subtítulos por la comunidad de Amara.org",
    "Subtítulos hechos por la comunidad de Amara.org",
    "Subtítulos por",
    "Subtítulos",
    "Subtítulos en español",
    "Amara.org",
    "www",
    # Despedidas y agradecimientos de cierre de video. "Gracias" suelto es lo
    # que devuelve large-v3-turbo ante 3 s de silencio, ruido o un tono, con
    # no_speech_prob 0.0: el filtro por segmentos no lo detecta (medido el
    # 2026-10-09; medium devuelve "" en esos casos).
    "Gracias por ver",
    "Gracias por ver el video",
    "Gracias por mirar",
    "Gracias",
    "Muchas gracias",
    "Hasta la próxima",
    "Nos vemos en el próximo video",
    # Llamadas a suscribirse
    "Suscríbete",
    "Suscríbete al canal",
    "Suscríbanse al canal",
    "No olvides suscribirte",
    "Dale like",
    # Anotaciones de sonido ("[Música]", "[Aplausos]", "[Risas]")
    "Música",
    "Música de fondo",
    "Aplausos",
    "Risas",
)


def _normalize_phrases(phrases: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """Frases como tuplas de unidades, sin vacías ni repetidas, en orden."""
    seen: dict[tuple[str, ...], None] = {}
    for phrase in phrases:
        units = tuple(to_units(phrase))
        if units:
            seen.setdefault(units, None)
    return tuple(seen)


# Lista negra normalizada: cada frase es una tupla de unidades de `to_units`.
BLACKLIST_ES: tuple[tuple[str, ...], ...] = _normalize_phrases(_HALLUCINATED_PHRASES_ES)

# Índice por primera unidad para recorrer la transcripción sin probar todas.
_BY_FIRST_UNIT: dict[str, tuple[tuple[str, ...], ...]] = {}
for _phrase in BLACKLIST_ES:
    _BY_FIRST_UNIT[_phrase[0]] = _BY_FIRST_UNIT.get(_phrase[0], ()) + (_phrase,)
del _phrase


@dataclass(frozen=True)
class FilterOutcome:
    """Resultado del filtro.

    `text` es lo que sigue hacia el matcher ("" si se descartó todo) y
    `reason` es el motivo del descarte, o None si el texto se conserva.
    `original` es el texto completo de Whisper antes de filtrar (registro y
    depuración: nunca debe llegar al matcher).
    """

    text: str
    reason: str | None
    original: str = ""

    @property
    def kept(self) -> bool:
        return self.reason is None


def clean_transcription(transcription: Any, *, near_text: str = "",
                        no_speech_threshold: float = 0.6,
                        logprob_threshold: float = -1.0,
                        compression_threshold: float = 2.4) -> FilterOutcome:
    """Filtra una transcripción de Whisper (`Transcription` del motor real o del falso).

    `transcription` es cualquier objeto con `.text` y `.segments` (cada uno
    con `text`, `no_speech_prob`, `avg_logprob` y `compression_ratio`). Sin
    segmentos se filtra directamente `.text`. `near_text` es el texto del
    guion alrededor de la posición actual: una frase de la lista negra que
    aparece ahí no se trata como alucinación.
    """
    segments = list(getattr(transcription, "segments", None) or ())
    original = _clean(getattr(transcription, "text", "") or "")

    first_drop: str | None = None
    if segments:
        kept_texts: list[str] = []
        for seg in segments:
            reason = _segment_drop_reason(seg, no_speech_threshold, logprob_threshold,
                                          compression_threshold)
            if reason is None:
                seg_text = _clean(getattr(seg, "text", "") or "")
                if seg_text:
                    kept_texts.append(seg_text)
            elif first_drop is None:
                first_drop = reason
        text = " ".join(kept_texts)
        if not original:
            original = " ".join(t for t in (_clean(getattr(s, "text", "") or "")
                                            for s in segments) if t)
    else:
        text = original

    units = to_units(text)
    if not units:
        return FilterOutcome("", first_drop or REASON_EMPTY, original)
    if _is_hallucination(units, near_text):
        return FilterOutcome("", REASON_BLACKLIST, original)
    if len(units) == 1 and is_stopword(units[0]):
        return FilterOutcome("", REASON_STOPWORD, original)
    return FilterOutcome(text, None, original)


# ---------------------------------------------------------------- interno

def _clean(text: str) -> str:
    """Espacios colapsados y recortados."""
    return " ".join(str(text).split())


def _metric(seg: Any, name: str) -> float:
    """Métrica numérica de un segmento; si falta, 0.0 (no provoca descartes)."""
    value = getattr(seg, name, None)
    return 0.0 if value is None else float(value)


def _segment_drop_reason(seg: Any, no_speech_threshold: float, logprob_threshold: float,
                         compression_threshold: float) -> str | None:
    """Motivo para descartar un segmento, o None si se conserva."""
    no_speech = _metric(seg, "no_speech_prob")
    logprob = _metric(seg, "avg_logprob")
    ratio = _metric(seg, "compression_ratio")
    if no_speech > no_speech_threshold and logprob < logprob_threshold:
        return REASON_NO_SPEECH
    if ratio > compression_threshold:
        return REASON_COMPRESSION
    return None


def _contains(seq: Sequence[str], sub: Sequence[str]) -> bool:
    """True si `sub` aparece de forma contigua dentro de `seq`."""
    n = len(sub)
    if n == 0 or n > len(seq):
        return False
    first = sub[0]
    return any(seq[i] == first and tuple(seq[i:i + n]) == tuple(sub)
               for i in range(len(seq) - n + 1))


def _only_phrases(units: Sequence[str], excluded: frozenset[tuple[str, ...]]) -> bool:
    """True si `units` se cubre entero con frases de la lista negra.

    Las frases pueden repetirse o combinarse y quedar unidas por stopwords
    sueltas ("gracias por ver y suscríbete"), pero tiene que haber al menos
    una frase. Las de `excluded` no cuentan. Programación dinámica: `state[i]`
    es None si el prefijo de largo i no se puede cubrir, False si solo se
    cubre con stopwords y True si se cubre con al menos una frase.
    """
    n = len(units)
    state: list[bool | None] = [None] * (n + 1)
    state[0] = False
    for i in range(n):
        current = state[i]
        if current is None:
            continue
        if is_stopword(units[i]):
            state[i + 1] = current if state[i + 1] is None else (state[i + 1] or current)
        for phrase in _BY_FIRST_UNIT.get(units[i], ()):
            end = i + len(phrase)
            if end <= n and phrase not in excluded and tuple(units[i:end]) == phrase:
                state[end] = True
    return state[n] is True


def _is_hallucination(units: Sequence[str], near_text: str) -> bool:
    """True si la transcripción es solo lista negra y esas frases no están en el guion cercano."""
    if not _only_phrases(units, frozenset()):
        return False  # caso habitual: hay palabras reales, ni se mira el guion
    near_units = to_units(near_text) if near_text else []
    in_script = frozenset(p for p in BLACKLIST_ES if _contains(near_units, p))
    if not in_script:
        return True
    # Las frases que el guion contiene cerca dejan de ser sospechosas.
    return _only_phrases(units, in_script)
