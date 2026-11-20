"""Matcher del guion: alinea palabras transcritas con las unidades de los tokens.

Interfaz común y algoritmo en docs/diseno_v2.md, sección 5. Esta es la
versión de la Fase 2 (matcher v2): alineamiento local por programación
dinámica (tipo Smith-Waterman) entre la transcripción de una ventana de audio
solapada (cada ciclo repite las palabras del anterior y suma las nuevas) y
las unidades de los tokens cercanos a `current_idx`.

Resumen de `ScriptMatcher.update`:

1. Ventana local: tokens `[current_idx - lookbehind, current_idx + lookahead]`
   aplanados a unidades, con un mapa unidad a token. Los tokens sin unidades
   (signos sueltos) no aportan ninguna.
2. Alineamiento local con los puntajes de `Scoring`: palabra de contenido
   exacta, stopword exacta (`text_norm.STOPWORDS_ES`), coincidencia difusa
   (Levenshtein 1 entre palabras de contenido de más de 3 letras),
   sustitución y hueco.
3. Se sigue la cola de lo dicho: el alineamiento óptimo debe terminar entre
   las últimas `tail_words` palabras de la transcripción. Si el óptimo termina
   antes (lo nuevo no coincide con lo que sigue), se prueba el mejor
   alineamiento que sí termina en la cola, pero solo se acepta si termina
   antes de `current_idx` (una relectura cercana): avanzar exige ser el
   óptimo, así una coincidencia suelta en la cola no arrastra un alineamiento
   viejo hacia adelante.
4. Se acepta si el puntaje llega a `min_score` y hay al menos una palabra de
   contenido alineada. `new_idx = último token alineado + 1`, saltando los
   tokens sin unidades; nunca baja y avanza como máximo `max_advance` tokens
   por llamada. Un alineamiento aceptado que termina antes de `current_idx`
   no avanza, pero cuenta como "en sincronía".
5. Las llamadas con palabras sin alineamiento aceptable suman un ciclo de
   estancamiento. Al llegar a `stall_cycles` se marca `desync` y se busca en
   `[current_idx - resync_lookbehind, current_idx + resync_lookahead]` un
   alineamiento fuerte (`resync_min_consecutive` palabras de contenido
   alineadas de forma consecutiva, terminando en la cola). Si existe, se
   salta ahí, adelante o atrás, con `resync=True`: es la única vía para
   retroceder. `desync` sigue activo en las llamadas fallidas siguientes (y
   en cada una se repite la búsqueda) hasta un alineamiento aceptado, una
   resincronización, `set_position` o `load`.

Rendimiento (Python 3.12 puro, máquina de desarrollo, 2026-10-09): una
llamada típica (10 palabras contra las 36 unidades de la ventana local) tarda
unos 0,07 ms (p50) y una llamada fallida con búsqueda de resincronización
(10 palabras contra 228 unidades) unos 0,4 ms; con 30 palabras, 1,1 ms. La
matriz guarda un byte por celda: `local_align` sobre una lectura completa de
546 palabras contra 546 unidades tarda unos 0,04 s.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from functools import lru_cache
from typing import Protocol

from tools.text_norm import STOPWORDS_ES, to_units

# Tolerancia para comparar puntajes: 1,2 y 0,6 no son exactos en binario y
# dos caminos con el mismo puntaje teórico pueden diferir en el último bit.
_EPS = 1e-9

# Solo se alinean las últimas palabras de la transcripción: una ventana de 3 s
# trae unas 10, y lo que importa es la cola. Acota el costo ante
# transcripciones anómalas (una ventana larga o un replay mal configurado).
MAX_WORDS = 48

# Tipos de par (palabra, unidad).
_NONE, _STOP, _CONTENT, _FUZZY = 0, 1, 2, 3

# Movimientos de la matriz de programación dinámica.
_START, _DIAG, _UP, _LEFT = 0, 1, 2, 3


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
    # Hito 84 (frenos de Voz): lo que de verdad se alineó en esta llamada.
    # `aligned_idx` = último token alineado + 1 (sin el tope `max_advance`;
    # None si no hubo alineamiento aceptable) y `run` = palabras seguidas
    # alineadas al final del alineamiento (sin huecos ni sustituciones).
    aligned_idx: int | None = None
    run: int = 0


@dataclass(frozen=True)
class Scoring:
    """Puntajes del alineamiento local (docs/diseno_v2.md, sección 5).

    Los de coincidencia deben ser positivos y los de sustitución y hueco,
    negativos: así un alineamiento local nunca empieza ni termina en un hueco.
    """

    content_exact: float = 2.0   # palabra de contenido exacta
    stop_exact: float = 0.5      # stopword exacta
    fuzzy: float = 1.2           # Levenshtein 1 entre palabras de contenido de más de 3 letras
    mismatch: float = -1.0       # sustitución
    gap: float = -0.6            # hueco (palabra o unidad sin pareja)

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{f.name} debe ser un número, no {type(value).__name__}")
            if not math.isfinite(value):
                raise ValueError(f"{f.name} debe ser finito")
            object.__setattr__(self, f.name, float(value))
        for name in ("content_exact", "stop_exact", "fuzzy"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} debe ser positivo")
        for name in ("mismatch", "gap"):
            if getattr(self, name) >= 0:
                raise ValueError(f"{name} debe ser negativo")


DEFAULT_SCORING = Scoring()


@dataclass(frozen=True)
class Alignment:
    """Resultado de `local_align`.

    `pairs` contiene solo las coincidencias (exactas o difusas) en orden,
    como (índice en words, índice en units); las sustituciones no aparecen.
    `max_consecutive_content` es la racha más larga de palabras de contenido
    alineadas sin sustituciones ni huecos entre ellas (las stopwords
    alineadas dentro de la racha no la cortan ni suman).
    """

    score: float = 0.0
    pairs: list[tuple[int, int]] = field(default_factory=list)
    content_matches: int = 0
    max_consecutive_content: int = 0
    word_end: int = -1     # índice en words de la última palabra alineada (-1: ninguna)
    unit_end: int = -1     # índice en units de la última unidad alineada (-1: ninguna)

    @property
    def tail_run(self) -> int:
        """Palabras alineadas seguidas (stopwords incluidas) que cierran el alineamiento."""
        pairs = self.pairs
        if not pairs:
            return 0
        n = 1
        for k in range(len(pairs) - 1, 0, -1):
            (wa, ua), (wb, ub) = pairs[k - 1], pairs[k]
            if (wb - wa, ub - ua) != (1, 1):
                break
            n += 1
        return n


# ---------------------------------------------------------------- alineamiento

def _one_edit(a: str, b: str) -> bool:
    """Levenshtein <= 1 entre palabras de más de 3 letras."""
    la, lb = len(a), len(b)
    if la <= 3 or lb <= 3 or abs(la - lb) > 1:
        return False
    if la == lb:
        diff = 0
        for x, y in zip(a, b):
            if x != y:
                diff += 1
                if diff > 1:
                    return False
        return True
    if la < lb:
        a, b = b, a
    i = 0
    while i < len(b) and a[i] == b[i]:
        i += 1
    return a[i + 1:] == b[i:]


@lru_cache(maxsize=1 << 16)
def _kind(word: str, unit: str) -> int:
    """Tipo de par entre una palabra transcrita y una unidad del guion.

    La coincidencia difusa solo vale entre palabras de contenido: una
    stopword "parecida" (`para` y `pare`) no aporta evidencia de posición.
    """
    if word == unit:
        return _STOP if unit in STOPWORDS_ES else _CONTENT
    if unit in STOPWORDS_ES or word in STOPWORDS_ES:
        return _NONE
    return _FUZZY if _one_edit(word, unit) else _NONE


class _Matrix:
    """Matriz de Smith-Waterman con hueco lineal entre `words` y `units`.

    Guarda solo un byte por celda (el movimiento: `_DIAG`, `_UP`, `_LEFT`, o
    `_START` si la celda vale 0; la columna 0 es virtual) y, por fila, la
    mejor celda de cierre como (puntaje, j). Solo cierran un alineamiento las
    celdas a las que se llega en diagonal por una coincidencia. Entre celdas
    de una fila empatadas gana la de menor j o, con `center`, la más cercana
    a esa unidad.
    """

    def __init__(self, words: Sequence[str], units: Sequence[str], scoring: Scoring,
                 center: int | None = None) -> None:
        self.words = words
        self.units = units
        m = len(units)
        table = (scoring.mismatch, scoring.stop_exact, scoring.content_exact, scoring.fuzzy)
        gap = scoring.gap
        distinct = set(units)
        kind_maps = {w: {u: _kind(w, u) for u in distinct} for w in set(words)}
        prev = [0.0] * (m + 1)
        self.moves: list[bytearray] = []
        self.row_best: list[tuple[float, int] | None] = []
        for w in words:
            kmap = kind_maps[w]
            krow = [kmap[u] for u in units]
            srow = [table[k] for k in krow]
            cur = [0.0] * (m + 1)
            move = bytearray(m + 1)
            left = 0.0
            best_s = 0.0
            best_j = -1
            for j in range(m):
                diag = prev[j] + srow[j]
                up = prev[j + 1] + gap
                lf = left + gap
                if diag > 0.0 and diag >= up and diag >= lf:
                    left = diag
                    move[j + 1] = _DIAG
                    if krow[j]:
                        if diag > best_s + _EPS:
                            best_s, best_j = diag, j
                        elif (center is not None and diag > best_s - _EPS
                              and abs(j - center) < abs(best_j - center)):
                            best_j = j
                elif up > 0.0 and up >= lf:
                    left = up
                    move[j + 1] = _UP
                elif lf > 0.0:
                    left = lf
                    move[j + 1] = _LEFT
                else:
                    left = 0.0
                cur[j + 1] = left
            self.moves.append(move)
            self.row_best.append((best_s, best_j) if best_j >= 0 else None)
            prev = cur

    def pick(self, first: int = 0) -> int:
        """Fila (desde `first`) con la mejor celda de cierre, o -1.

        En empate gana la fila más tardía.
        """
        best_i = -1
        best_s = 0.0
        for i in range(max(0, first), len(self.row_best)):
            rb = self.row_best[i]
            if rb is not None and (best_i < 0 or rb[0] > best_s - _EPS):
                best_i = i
                best_s = max(best_s, rb[0])
        return best_i

    def alignment(self, i: int) -> Alignment:
        """Reconstruye el alineamiento que cierra en la mejor celda de la fila `i`."""
        score, j = self.row_best[i]  # type: ignore[misc]
        words, units, moves = self.words, self.units, self.moves
        pairs: list[tuple[int, int]] = []
        while i >= 0 and j >= 0:
            mv = moves[i][j + 1]
            if mv == _DIAG:
                if _kind(words[i], units[j]):
                    pairs.append((i, j))
                i -= 1
                j -= 1
            elif mv == _UP:
                i -= 1
            elif mv == _LEFT:
                j -= 1
            else:
                break
        pairs.reverse()
        content = 0
        run = 0
        max_run = 0
        prev_pair = (-2, -2)
        for wi, uj in pairs:
            if (wi, uj) != (prev_pair[0] + 1, prev_pair[1] + 1):
                run = 0
            if _kind(words[wi], units[uj]) != _STOP:
                content += 1
                run += 1
                max_run = max(max_run, run)
            prev_pair = (wi, uj)
        return Alignment(score=score, pairs=pairs, content_matches=content,
                         max_consecutive_content=max_run,
                         word_end=pairs[-1][0], unit_end=pairs[-1][1])


def local_align(words: Sequence[str], units: Sequence[str], *,
                scoring: Scoring | None = None) -> Alignment:
    """Alineamiento local óptimo (Smith-Waterman) de `words` contra `units`.

    Ambas secuencias ya normalizadas con `to_units`. Entre alineamientos de
    igual puntaje gana el que termina más tarde en `words` y, después, el que
    termina antes en `units`. Sin ninguna coincidencia devuelve un
    `Alignment()` vacío (puntaje 0, `word_end` y `unit_end` en -1).
    """
    scoring = DEFAULT_SCORING if scoring is None else scoring
    words = list(words)
    units = list(units)
    if not words or not units:
        return Alignment()
    mat = _Matrix(words, units, scoring)
    i = mat.pick()
    return Alignment() if i < 0 else mat.alignment(i)


# ---------------------------------------------------------------- parámetros

_INT_MINIMUMS: dict[str, int] = {
    "lookbehind": 0,               # tokens antes de current_idx en la ventana local
    "lookahead": 1,                # tokens después de current_idx en la ventana local
    "max_advance": 1,              # tope de avance (en tokens) por llamada
    "tail_words": 1,               # el alineamiento debe terminar entre las últimas N palabras
    "stall_cycles": 1,             # llamadas fallidas seguidas antes de marcar desync
    "resync_lookbehind": 0,        # tokens antes de current_idx en la búsqueda de resync
    "resync_lookahead": 0,         # tokens después de current_idx en la búsqueda de resync
    "resync_min_consecutive": 1,   # palabras de contenido consecutivas para resincronizar
}

DEFAULT_PARAMS: dict[str, float] = {
    "lookbehind": 10,
    "lookahead": 25,
    "min_score": 3.0,              # puntaje mínimo de un alineamiento aceptable
    "max_advance": 8,
    "tail_words": 3,
    "stall_cycles": 6,
    "resync_lookbehind": 40,
    "resync_lookahead": 200,
    "resync_min_consecutive": 4,
    **{f.name: f.default for f in fields(Scoring)},
}


def _check_params(params: dict) -> dict[str, float]:
    """Valida y completa los parámetros; los desconocidos lanzan TypeError."""
    unknown = set(params) - set(DEFAULT_PARAMS)
    if unknown:
        raise TypeError(f"parámetros desconocidos del matcher: {', '.join(sorted(unknown))}")
    merged = {**DEFAULT_PARAMS, **params}
    out: dict[str, float] = {}
    for name, value in merged.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} debe ser un número, no {type(value).__name__}")
        if not math.isfinite(value):
            raise ValueError(f"{name} debe ser finito")
        if name in _INT_MINIMUMS:
            if value != int(value):
                raise ValueError(f"{name} debe ser entero")
            value = int(value)
            if value < _INT_MINIMUMS[name]:
                raise ValueError(f"{name} debe ser >= {_INT_MINIMUMS[name]}")
        else:
            value = float(value)
        out[name] = value
    if out["min_score"] < 0:
        raise ValueError("min_score debe ser >= 0")
    return out


# ---------------------------------------------------------------- matcher

class ScriptMatcher:
    """Matcher v2: alineamiento local, monótono, con desincronización y resync."""

    def __init__(self, tokens: Sequence[_TokenLike] | None = None, **params) -> None:
        self.params = _check_params(params)
        self.scoring = Scoring(**{f.name: self.params[f.name] for f in fields(Scoring)})
        self._tokens: tuple[_TokenLike, ...] = ()
        self._units: list[str] = []       # unidades de todo el guion, aplanadas
        self._owner: list[int] = []       # token de cada unidad
        self._tok_start: list[int] = [0]  # primera unidad de cada token (len = total + 1)
        self._idx = 0
        self._stall = 0
        self._desync = False
        self._lock = threading.Lock()
        if tokens is not None:
            self.load(tokens)

    # -------------------------------------------------------- estado

    @property
    def current_idx(self) -> int:
        return self._idx

    @property
    def total_tokens(self) -> int:
        return len(self._tokens)

    @property
    def tokens(self) -> Sequence[_TokenLike]:
        """Tokens del guion cargado (tupla de solo lectura)."""
        return self._tokens

    @property
    def desync(self) -> bool:
        """True mientras el matcher está desincronizado."""
        return self._desync

    def load(self, tokens: Sequence[_TokenLike]) -> None:
        """Carga los tokens del guion y deja la posición en 0."""
        toks = tuple(tokens)
        units: list[str] = []
        owner: list[int] = []
        starts: list[int] = []
        for t, tok in enumerate(toks):
            starts.append(len(units))
            for unit in tok.units:
                if unit:
                    units.append(unit)
                    owner.append(t)
        starts.append(len(units))
        with self._lock:
            self._tokens = toks
            self._units = units
            self._owner = owner
            self._tok_start = starts
            self._idx = 0
            self._stall = 0
            self._desync = False

    def set_position(self, idx: int) -> int:
        """Recorta `idx` a [0, total], lo aplica y devuelve el valor aplicado.

        Limpia el estancamiento y la desincronización.
        """
        with self._lock:
            self._idx = max(0, min(int(idx), len(self._tokens)))
            self._stall = 0
            self._desync = False
            return self._idx

    def context_before(self, n_words: int = 25) -> str:
        """Textos de los `n_words` tokens anteriores a `current_idx` (para `initial_prompt`)."""
        with self._lock:
            if n_words <= 0:
                return ""
            cur = self._idx
            return " ".join(t.text for t in self._tokens[max(0, cur - n_words):cur])

    def text_near(self, before: int = 10, after: int = 50) -> str:
        """Textos de los tokens en `[current_idx - before, current_idx + after)`.

        Para el filtro de alucinaciones (`near_text`).
        """
        with self._lock:
            cur = self._idx
            lo = max(0, cur - max(0, before))
            hi = min(len(self._tokens), cur + max(0, after))
            return " ".join(t.text for t in self._tokens[lo:hi])

    # -------------------------------------------------------- update

    def update(self, words: Sequence[str]) -> MatchResult:
        """Alinea las palabras (ya normalizadas con `to_units`) y avanza si procede."""
        if isinstance(words, str):  # comodidad: texto crudo
            words = to_units(words)
        words = [w for w in words if w][-MAX_WORDS:]
        with self._lock:
            cur = self._idx
            if not self._tokens:
                return MatchResult(cur, False, 0.0, None)
            if not words:
                # Transcripción vacía o filtrada: no cuenta como ciclo.
                return MatchResult(cur, False, 0.0, None, desync=self._desync)
            sc = self.scoring
            best_possible = sum(sc.stop_exact if w in STOPWORDS_ES else sc.content_exact
                                for w in words)

            aln, u0, score = self._local(words, cur)
            if aln is not None:
                last_tok = self._owner[u0 + aln.unit_end]
                new_idx = cur
                if last_tok + 1 > cur:
                    cap = cur + self.params["max_advance"]
                    new_idx = self._skip_unitless(min(last_tok + 1, cap))
                self._idx = new_idx
                self._stall = 0
                self._desync = False
                return MatchResult(new_idx, new_idx > cur, _conf(aln.score, best_possible),
                                   self._tokens[last_tok].text,
                                   aligned_idx=self._skip_unitless(last_tok + 1),
                                   run=aln.tail_run)

            self._stall += 1
            if self._stall >= self.params["stall_cycles"]:
                self._desync = True
                found = self._resync(words, cur, best_possible)
                if found is not None:
                    return found
            return MatchResult(cur, False, _conf(score, best_possible), None,
                               desync=self._desync)

    # -------------------------------------------------------- internos

    def _window(self, cur: int, behind: int, ahead: int) -> tuple[int, list[str]]:
        """Unidades de los tokens `[cur - behind, cur + ahead]` y el índice de la primera."""
        total = len(self._tokens)
        lo = max(0, cur - behind)
        hi = min(total, cur + ahead + 1)
        u0, u1 = self._tok_start[lo], self._tok_start[max(lo, hi)]
        return u0, self._units[u0:u1]

    def _skip_unitless(self, idx: int) -> int:
        """Primer índice >= idx con unidades (o el total): los signos sueltos no se leen."""
        starts = self._tok_start
        total = len(self._tokens)
        while idx < total and starts[idx + 1] == starts[idx]:
            idx += 1
        return idx

    def _local(self, words: list[str], cur: int) -> tuple[Alignment | None, int, float]:
        """Alineamiento aceptable en la ventana local, su primera unidad y el puntaje visto."""
        p = self.params
        u0, units = self._window(cur, p["lookbehind"], p["lookahead"])
        if not units:
            return None, u0, 0.0
        mat = _Matrix(words, units, self.scoring)
        g = mat.pick()
        if g < 0:
            return None, u0, 0.0
        tail_from = len(words) - p["tail_words"]
        if g >= tail_from:
            aln = mat.alignment(g)
        else:
            # El óptimo no sigue la cola: solo se acepta una relectura cercana
            # (un alineamiento que cierra en la cola y antes de current_idx).
            best = mat.row_best[g][0]  # type: ignore[index]
            t = mat.pick(tail_from)
            if t < 0:
                return None, u0, best
            aln = mat.alignment(t)
            if self._owner[u0 + aln.unit_end] >= cur:
                return None, u0, best
        if aln.score < p["min_score"] - _EPS or aln.content_matches < 1:
            return None, u0, aln.score
        return aln, u0, aln.score

    def _resync(self, words: list[str], cur: int, best_possible: float) -> MatchResult | None:
        """Busca un alineamiento fuerte en la ventana amplia y salta ahí si existe."""
        p = self.params
        u0, units = self._window(cur, p["resync_lookbehind"], p["resync_lookahead"])
        if not units:
            return None
        center = self._tok_start[cur] - u0  # unidad del cursor: desempata hacia ella
        mat = _Matrix(words, units, self.scoring, center=center)
        t = mat.pick(len(words) - p["tail_words"])
        if t < 0:
            return None
        aln = mat.alignment(t)
        if (aln.max_consecutive_content < p["resync_min_consecutive"]
                or aln.score < p["min_score"] - _EPS):
            return None
        last_tok = self._owner[u0 + aln.unit_end]
        target = self._skip_unitless(last_tok + 1)
        self._stall = 0
        self._desync = False
        conf = _conf(aln.score, best_possible)
        word = self._tokens[last_tok].text
        if target == cur:  # ya estaba ahí: solo vuelve a la sincronía
            return MatchResult(cur, False, conf, word, aligned_idx=target, run=aln.tail_run)
        self._idx = target
        return MatchResult(target, target > cur, conf, word, resync=True,
                           aligned_idx=target, run=aln.tail_run)


def _conf(score: float, best_possible: float) -> float:
    """Puntaje normalizado por el máximo posible de la transcripción, en 0..1."""
    if best_possible <= 0:
        return 0.0
    return round(min(1.0, max(0.0, score / best_possible)), 3)
