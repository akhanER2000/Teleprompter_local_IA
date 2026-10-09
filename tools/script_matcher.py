"""Matcher del guion: alinea palabras transcritas con las unidades de los tokens.

Interfaz común en docs/diseno_v2.md, sección 5. Esta es la versión de la
Fase 1: el algoritmo voraz anterior (primera coincidencia hacia adelante en
una ventana corta) adaptado a unidades de `tools.text_norm`. La Fase 2 la
reemplaza por un alineamiento local detrás de la misma interfaz.
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tools.text_norm import STOPWORDS_ES, to_units

# Parámetros del algoritmo voraz (los valores de la versión anterior, salvo
# el modo "sticky", que queda desactivado).
#
# Ventana y tope crecen con la transcripción: con inferencia lenta (CPU, o el
# fallback de CUDA a CPU) cada bloque trae 4 a 8 palabras, y con una ventana o
# un tope fijos menores que el bloque el matcher se queda atrás y no se
# recupera (medido sobre guiones/ejemplo.txt). En una llamada con n palabras
# la ventana mira max(lookahead, 2n) tokens hacia adelante y el tope es
# max(max_advance, n + 2).
#
# El modo "sticky" (ventana estrecha tras set_position) mitigaba los resultados
# viejos que llegaban tras reposicionar (C4); ahora los descartan la generación
# de la sesión y `discard_audio`, y con bloques de más de 3 palabras detenía
# la sincronía para siempre. Se conserva como parámetro (sticky_updates > 0)
# para que el replay pueda medirlo.
DEFAULT_PARAMS: dict[str, float] = {
    "lookbehind": 3,         # tokens antes de current_idx que entran en la ventana
    "lookahead": 12,         # tokens desde current_idx hacia adelante (mínimo)
    "min_confidence": 0.5,   # fracción ponderada de palabras alineadas
    "max_advance": 6,        # tope de avance (en tokens) por llamada a update (mínimo)
    "fuzzy_weight": 0.7,     # peso de una coincidencia difusa (la exacta vale 1)
    "sticky_lookahead": 3,   # ventana y tope mínimos tras set_position (modo sticky)
    "sticky_updates": 0,     # llamadas a update en modo sticky tras set_position (0: nunca)
    "anchor_min_len": 3,     # largo mínimo de la palabra de contenido que ancla
}
_INT_PARAMS = {"lookbehind", "lookahead", "max_advance", "sticky_lookahead",
               "sticky_updates", "anchor_min_len"}


class _TokenLike(Protocol):
    text: str
    units: list[str]


@dataclass
class MatchResult:
    new_idx: int
    advanced: bool
    confidence: float          # 0..1
    matched_word: str | None   # texto del último token alineado
    resync: bool = False       # salto por resincronización (Fase 2)
    desync: bool = False       # el matcher está desincronizado (Fase 2)


def _fuzzy_eq(a: str, b: str) -> bool:
    """Levenshtein <= 1 entre palabras de más de 3 letras."""
    if a == b:
        return True
    if len(a) <= 3 or len(b) <= 3 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


class ScriptMatcher:
    """Matcher voraz por unidades: monótono y con tope de avance por llamada."""

    def __init__(self, tokens: Sequence[_TokenLike] | None = None, **params) -> None:
        unknown = set(params) - set(DEFAULT_PARAMS)
        if unknown:
            raise TypeError(f"parámetros desconocidos del matcher: {', '.join(sorted(unknown))}")
        merged = {**DEFAULT_PARAMS, **params}
        self.params = {k: int(v) if k in _INT_PARAMS else float(v) for k, v in merged.items()}
        self._tokens: list[_TokenLike] = []
        self._idx = 0
        self._sticky_remaining = 0
        self._lock = threading.Lock()
        if tokens is not None:
            self.load(tokens)

    @property
    def current_idx(self) -> int:
        return self._idx

    @property
    def total_tokens(self) -> int:
        return len(self._tokens)

    def load(self, tokens: Sequence[_TokenLike]) -> None:
        """Carga los tokens del guion y deja la posición en 0."""
        with self._lock:
            self._tokens = list(tokens)
            self._idx = 0
            self._sticky_remaining = 0

    def set_position(self, idx: int) -> int:
        """Recorta `idx` a [0, total], lo aplica y devuelve el valor aplicado.

        Con `sticky_updates` > 0, esas siguientes llamadas a `update` usan una
        ventana estrecha (desactivado por defecto).
        """
        with self._lock:
            self._idx = max(0, min(int(idx), len(self._tokens)))
            self._sticky_remaining = self.params["sticky_updates"]
            return self._idx

    def update(self, words: Sequence[str]) -> MatchResult:
        """Alinea las palabras (ya normalizadas con `to_units`) y avanza si procede."""
        if isinstance(words, str):  # comodidad: texto crudo
            words = to_units(words)
        words = [w for w in words if w]
        with self._lock:
            cur = self._idx
            if not self._tokens or not words:
                return MatchResult(cur, False, 0.0, None)
            p = self.params
            n = len(words)
            if self._sticky_remaining > 0:
                self._sticky_remaining -= 1
                # Estrecha, pero nunca menor que el bloque: si no, no avanza nunca.
                lookahead = cap = max(p["sticky_lookahead"], n + 2)
            else:
                lookahead = max(p["lookahead"], 2 * n)
                cap = max(p["max_advance"], n + 2)
            total = len(self._tokens)

            # Ventana aplanada a unidades, con el índice del token de cada una.
            units: list[str] = []
            owners: list[int] = []
            for t in range(max(0, cur - p["lookbehind"]), min(total, cur + lookahead)):
                for unit in self._tokens[t].units:
                    units.append(unit)
                    owners.append(t)

            # Voraz: cada palabra toma la primera unidad que coincide después de
            # la anterior coincidencia.
            probe = 0
            hits = 0.0
            last_tok = -1
            anchored = False
            for w in words:
                for j in range(probe, len(units)):
                    if units[j] == w:
                        hits += 1.0
                    elif _fuzzy_eq(units[j], w):
                        hits += p["fuzzy_weight"]
                    else:
                        continue
                    probe = j + 1
                    last_tok = owners[j]
                    if len(w) >= p["anchor_min_len"] and w not in STOPWORDS_ES:
                        anchored = True
                    break

            confidence = round(min(1.0, hits / len(words)), 3)
            matched_word = self._tokens[last_tok].text if last_tok >= 0 else None
            new_idx = cur
            if confidence >= p["min_confidence"] and anchored and last_tok + 1 > cur:
                new_idx = min(last_tok + 1, cur + cap, total)
                # Los tokens sin unidades (signos sueltos) no se leen: se saltan.
                while new_idx < total and not self._tokens[new_idx].units:
                    new_idx += 1
            self._idx = new_idx
            return MatchResult(new_idx, new_idx > cur, confidence, matched_word)
