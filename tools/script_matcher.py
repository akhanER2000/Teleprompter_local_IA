"""Matcher determinista: alinea transcripción con guion y devuelve nuevo índice."""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return s.strip()


@dataclass
class Token:
    idx: int
    word: str   # render original
    norm: str   # normalizado para match


def tokenize_script(text: str) -> list[Token]:
    tokens: list[Token] = []
    idx = 0
    for raw in text.split():
        n = normalize(raw)
        if not n:
            continue
        tokens.append(Token(idx=idx, word=raw, norm=n))
        idx += 1
    return tokens


def _fuzzy_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) <= 3 or len(b) <= 3:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    # Levenshtein <= 1 con corte rápido
    if len(a) == len(b):
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        return diffs <= 1
    # diferencia de longitud 1: chequear inserción/borrado
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(longer)):
        candidate = longer[:i] + longer[i + 1:]
        if candidate == shorter:
            return True
    return False


@dataclass
class MatchResult:
    new_idx: int
    confidence: float
    matched_word: str | None


class ScriptMatcher:
    LOOKAHEAD = 12
    LOOKBEHIND = 3
    MIN_CONFIDENCE = 0.5
    MAX_ADVANCE_PER_CHUNK = 6
    STOPWORDS = {
        "y", "o", "a", "e", "de", "del", "la", "el", "los", "las", "un",
        "una", "unos", "unas", "que", "en", "es", "se", "lo", "le", "su",
        "al", "por", "con", "para", "ya", "si", "no",
    }

    STICKY_LOOKAHEAD = 3
    STICKY_CHUNKS = 4  # nº de chunks tras un reset manual con ventana estrecha

    def __init__(self):
        self.tokens: list[Token] = []
        self.current_idx: int = 0
        self.sticky_remaining: int = 0

    def load_script(self, text: str):
        self.tokens = tokenize_script(text)
        self.current_idx = 0
        self.sticky_remaining = 0

    def reset(self, idx: int = 0):
        self.current_idx = max(0, min(idx, len(self.tokens)))
        # Tras un reset manual, las próximas N transcripciones se anclan cerca:
        # no se permite saltar hacia adelante más allá de STICKY_LOOKAHEAD.
        self.sticky_remaining = self.STICKY_CHUNKS

    def update(self, transcript: str) -> MatchResult:
        if not self.tokens or not transcript.strip():
            return MatchResult(self.current_idx, 0.0, None)

        words = [w for w in normalize(transcript).split() if w]
        if not words:
            return MatchResult(self.current_idx, 0.0, None)

        # Ventana estrecha tras un reset manual: el matcher solo acepta
        # avances pegados al current_idx, evitando que el buffer de audio
        # acumulado salte de nuevo hacia adelante.
        if self.sticky_remaining > 0:
            lookahead = self.STICKY_LOOKAHEAD
            self.sticky_remaining -= 1
        else:
            lookahead = self.LOOKAHEAD

        start = max(0, self.current_idx - self.LOOKBEHIND)
        end = min(len(self.tokens), self.current_idx + lookahead)
        window = self.tokens[start:end]

        probe = 0
        hits = 0.0
        last_match_local = -1
        last_matched_word = None

        for w in words:
            found_at = -1
            for j in range(probe, len(window)):
                if window[j].norm == w:
                    found_at = j
                    hits += 1.0
                    last_matched_word = window[j].word
                    break
                if _fuzzy_eq(window[j].norm, w):
                    found_at = j
                    hits += 0.7
                    last_matched_word = window[j].word
                    break
            if found_at >= 0:
                probe = found_at + 1
                last_match_local = found_at

        confidence = hits / max(1, len(words))

        # Anti-saltos: requerir al menos un "anchor" (palabra no-stopword) entre los matches
        non_stopword_hits = sum(
            1 for w in words
            if w not in self.STOPWORDS and len(w) > 2 and any(
                tok.norm == w or _fuzzy_eq(tok.norm, w) for tok in window
            )
        )

        if (confidence >= self.MIN_CONFIDENCE
                and last_match_local >= 0
                and non_stopword_hits >= 1):
            new_idx = start + last_match_local + 1
            # monotonicidad + cap por chunk
            new_idx = max(new_idx, self.current_idx)
            cap = (self.STICKY_LOOKAHEAD if self.sticky_remaining > 0
                   else self.MAX_ADVANCE_PER_CHUNK)
            new_idx = min(new_idx, self.current_idx + cap)
            new_idx = min(new_idx, len(self.tokens))
            self.current_idx = new_idx

        return MatchResult(
            new_idx=self.current_idx,
            confidence=round(confidence, 3),
            matched_word=last_matched_word,
        )
