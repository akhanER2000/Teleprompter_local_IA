"""Frenos del modo Voz (hito 84, T4): deciden qué posición se MUESTRA.

El matcher sigue a la voz como siempre; los frenos se ponen entre su
resultado y la pantalla, con cuatro reglas (todas configurables):

1. **Confirmación.** El cursor solo se mueve hacia una posición respaldada
   por al menos `confirm_words` palabras alineadas seguidas (`MatchResult.run`)
   en DOS ciclos consecutivos (las ventanas de audio se solapan: lo dicho de
   verdad aparece en varias; una palabra que Whisper inventó al completar la
   frase casi nunca se repite igual). Se toma la menor de las dos posiciones.
2. **Adelanto máximo.** Una confirmación nunca deja el objetivo más de
   `max_ahead` tokens por delante de la confirmación anterior: un salto mayor
   se completa en las confirmaciones siguientes.
3. **Velocidad máxima.** El cursor visible avanza hacia el objetivo a
   `max_wps` palabras por segundo como mucho (un token por paso, repartido en
   el tiempo con `tick`), en vez de pegar el salto de golpe.
4. **Frase.** No se da por terminada una frase que no se dijo: si la
   confirmación cierra justo en la última palabra de una frase, el cursor se
   queda en esa palabra hasta que otra confirmación la repita o llegue
   alguna palabra de la frase siguiente. Whisper tiende a completar frases
   que el orador todavía no terminó.

Una resincronización (alineamiento fuerte lejos del cursor) no se frena: es
el orador saltando a otra parte del guion, y se aplica de inmediato.

Sin estado global ni reloj propio: `tick(now)` recibe el tiempo, así las
pruebas son deterministas.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

_SENTENCE_END = (".", "?", "!", "…", ".»", "?»", "!»", '."', '?"', '!"', ".)", "?)", "!)")


class _TokenLike(Protocol):
    text: str
    units: list[str]


@dataclass(frozen=True)
class BrakeParams:
    """Valores de los frenos. Rangos en `LIMITS`."""

    confirm_words: int = 3
    max_ahead: int = 6
    max_wps: float = 6.0          # 360 palabras por minuto (medido: 4,5 retrasaba la puesta al día)
    sentence_guard: bool = True
    # Una observación sólida que ningún ciclo posterior contradice se confirma sola
    # pasado este tiempo. Sin esto, la última palabra antes de una pausa esperaba
    # el final de la pausa (en el silencio Whisper no devuelve nada que la respalde):
    # medido en la lectura de V0, +2,9 s de retraso p95.
    confirm_hold_s: float = 0.6
    # Cuánto puede la observación nueva pasar a la anterior al confirmar (palabras):
    # 0 = acuerdo estricto (un ciclo de retraso siempre).
    agree_slack: int = 2
    # El tope de adelanto se mide desde la observación sólida MÁS BAJA de esta
    # ventana, no solo desde lo confirmado: si no, dos completaciones de Whisper
    # confirmadas una tras otra suben la base del tope y el adelanto se encadena.
    anchor_window_s: float = 2.0

    def __post_init__(self) -> None:
        for name, (lo, hi) in LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} debe ser un número")
            if not lo <= value <= hi:
                raise ValueError(f"{name} fuera de rango: {value} (válido: {lo} a {hi})")
        if not isinstance(self.sentence_guard, bool):
            raise TypeError("sentence_guard debe ser true o false")


LIMITS: dict[str, tuple[float, float]] = {
    "confirm_words": (1, 10),
    "max_ahead": (1, 30),
    "max_wps": (0.5, 10.0),   # con tick cada 0,1 s, 10 es el techo real
    "confirm_hold_s": (0.2, 5.0),
    "agree_slack": (0, 6),
    "anchor_window_s": (0.0, 10.0),
}


def is_sentence_end(text: str) -> bool:
    t = text.rstrip()
    return t.endswith(_SENTENCE_END)


class VoiceBrakes:
    """Posición visible del modo Voz. Ver el docstring del módulo."""

    def __init__(self, tokens: Sequence[_TokenLike], params: BrakeParams | None = None,
                 start_idx: int = 0, now: float = 0.0) -> None:
        self.params = params or BrakeParams()
        self._tokens = list(tokens)
        self._total = len(self._tokens)
        self._unitless = [not t.units for t in self._tokens]
        self._end = [is_sentence_end(t.text) for t in self._tokens]
        self.reset(start_idx, now)

    # ------------------------------------------------------------ estado

    @property
    def displayed(self) -> int:
        """Índice que ve el lector."""
        return self._shown

    @property
    def confirmed(self) -> int:
        """Última posición confirmada (con el adelanto máximo aplicado)."""
        return self._confirmed

    @property
    def goal(self) -> int:
        """Hacia dónde camina el cursor visible."""
        return self._goal

    def reset(self, idx: int, now: float = 0.0) -> None:
        """Reposicionamiento (set_position, start, carga): todo vuelve a `idx`."""
        idx = max(0, min(self._total, int(idx)))
        self._shown = self._confirmed = self._goal = idx
        self._held_end: int | None = None   # frase cuyo final espera una segunda confirmación
        self._prev: int | None = None       # posición respaldada en el ciclo anterior
        self._prev_t = now                  # cuándo se observó
        self._recent: list[tuple[float, int]] = []   # observaciones sólidas recientes (t, idx)
        self._budget = 0.0
        self._last_tick = now

    # ------------------------------------------------------------ entrada

    def observe(self, aligned_idx: int | None, run: int, *, resync: bool = False,
                now: float = 0.0) -> bool:
        """Registra un resultado del matcher. Devuelve True si fue un salto inmediato (resync)."""
        if aligned_idx is None:
            return False
        target = max(0, min(self._total, int(aligned_idx)))
        if resync:
            self.reset(target, now)
            return True
        p = self.params
        if run < p.confirm_words:
            self._prev = None
            return False
        w = p.anchor_window_s
        self._recent = [(t, x) for t, x in self._recent if now - t <= w] + [(now, target)]
        prev, self._prev, self._prev_t = self._prev, target, now
        if prev is None:
            return False                  # primer ciclo que la respalda: espera al siguiente
        self._confirm(min(target, prev + p.agree_slack))
        return False

    def _confirm(self, target: int) -> None:
        """Da por dicha la posición `target` (con el adelanto máximo y la guarda de frase)."""
        p = self.params
        if target <= self._confirmed:
            return
        target = min(target, self._confirmed + p.max_ahead)
        if self._recent:
            target = min(target, min(x for _, x in self._recent) + p.max_ahead)
        if p.sentence_guard:
            target = self._guard_sentence(target)
        if target > self._confirmed:
            self._confirmed = target
            self._goal = max(self._goal, target)

    def _guard_sentence(self, target: int) -> int:
        """Si el objetivo pasa el final de una frase recién cerrado, se detiene en él."""
        last = target - 1                 # último token confirmado
        while last >= 0 and self._unitless[last]:
            last -= 1
        if last < 0 or not self._end[last] or last < self._confirmed:
            self._held_end = None
            return target
        # La confirmación cierra justo en el final de una frase.
        if self._held_end == last:
            self._held_end = None         # segunda vez: ahora sí se da por dicha
            return target
        self._held_end = last
        return last                       # el cursor se queda en la última palabra

    # ------------------------------------------------------------ reloj

    def tick(self, now: float) -> int | None:
        """Avanza el cursor visible según la velocidad máxima. Devuelve el índice nuevo o None."""
        dt = max(0.0, now - self._last_tick)
        self._last_tick = now
        if (self._prev is not None and self._prev > self._confirmed
                and now - self._prev_t >= self.params.confirm_hold_s):
            self._confirm(self._prev)     # nadie la contradijo: se da por dicha
        if self._shown >= self._goal:
            self._budget = 0.0            # parado: no acumula crédito para una ráfaga
            return None
        # El resto pasa al tick siguiente (sin él, 4,5 pal/s con ticks de 0,1 s rendían
        # 3,3); el tope de 1,0 tras restar impide acumular para una ráfaga.
        self._budget += dt * self.params.max_wps
        if self._budget < 1.0 - 1e-9:   # tolera el redondeo de 0,1 + 0,1 + …
            return None
        self._budget = min(max(0.0, self._budget - 1.0), 1.0)
        idx = self._shown + 1
        while idx < self._goal and self._unitless[idx]:
            idx += 1
        self._shown = min(idx, self._goal)
        return self._shown
