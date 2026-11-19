"""Matcher v2 de la Fase 2: alineamiento local por unidades (docs/diseno_v2.md, sección 5).

Las lecturas se simulan como en vivo: cada llamada a `update` recibe la
transcripción de una ventana solapada, es decir, las últimas palabras dichas
(las del ciclo anterior más 1 a 3 nuevas).
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from tools.script_matcher import (
    DEFAULT_PARAMS, MAX_WORDS, Alignment, MatchResult, Scoring, ScriptMatcher, local_align,
)
from tools.script_parser import parse_script
from tools.text_norm import to_units

EM_DASH = "\u2014"

GUION = (
    "Buenas tardes a todos y bienvenidos a esta presentación. Hoy vamos a conocer el nuevo "
    "sistema de reconocimiento de voz que desarrollamos durante el último año en nuestro "
    "laboratorio.\n\n"
    "Primero revisaremos la arquitectura general, después analizaremos los componentes "
    "principales del motor y finalmente comentaremos los resultados obtenidos en las pruebas "
    "con usuarios reales de distintas regiones.\n\n"
    "La tarjeta gráfica procesa cada ventana de audio en pocos milisegundos, mientras el "
    "alineador compara las palabras transcritas con el guion para decidir la posición exacta "
    "del lector.\n\n"
    "Para terminar, muchas gracias por su atención."
)
TOKENS = parse_script(GUION).tokens
TEXTS = [t.text for t in TOKENS]
P2 = TEXTS.index("Primero")      # inicio del segundo párrafo
P3 = TEXTS.index("La")           # inicio del tercer párrafo
P4 = TEXTS.index("Para")         # inicio del cierre
TOTAL = len(TOKENS)

EJEMPLO = Path(__file__).resolve().parent.parent / "guiones" / "ejemplo.txt"


def _matcher(text: str, **params) -> ScriptMatcher:
    return ScriptMatcher(parse_script(text).tokens, **params)


def _say(m: ScriptMatcher, text: str) -> MatchResult:
    return m.update(to_units(text))


class Lector:
    """Simula al orador: `spoken` son los índices de token dichos, en orden.

    Cada `say` agrega tokens y transcribe la ventana solapada: los textos de
    los últimos `keep` tokens dichos (unos 3 s de voz).
    """

    def __init__(self, m: ScriptMatcher, tokens=TOKENS, keep: int = 9) -> None:
        self.m = m
        self.tokens = tokens
        self.keep = keep
        self.spoken: list[int] = []

    def say(self, idxs) -> MatchResult:
        self.spoken.extend(idxs)
        window = self.spoken[-self.keep:]
        return self.m.update(to_units(" ".join(self.tokens[i].text for i in window)))

    def read(self, start: int, stop: int, rnd: random.Random | None = None,
             step: tuple[int, int] = (1, 3)) -> list[tuple[int, MatchResult]]:
        """Lee los tokens [start, stop), de 1 a 3 nuevos por ventana.

        Devuelve una lista de (tokens dichos hasta ahí, resultado).
        """
        rnd = rnd or random.Random(0)
        out = []
        i = start
        while i < stop:
            k = min(stop - i, rnd.randint(*step))
            r = self.say(range(i, i + k))
            i += k
            out.append((i, r))
        return out


# ---------------------------------------------------------------- local_align

def test_local_align_coincidencia_exacta() -> None:
    a = local_align(["presentar", "el", "nuevo"], ["presentar", "el", "nuevo"])
    assert a.score == pytest.approx(2.0 + 0.5 + 2.0)
    assert a.pairs == [(0, 0), (1, 1), (2, 2)]
    assert (a.content_matches, a.max_consecutive_content) == (2, 2)
    assert (a.word_end, a.unit_end) == (2, 2)


def test_local_align_es_local_ignora_los_extremos() -> None:
    a = local_align(["zzz", "presentar", "el", "nuevo", "yyy"],
                    ["hoy", "vamos", "presentar", "el", "nuevo", "sistema"])
    assert a.score == pytest.approx(4.5)
    assert a.pairs == [(1, 2), (2, 3), (3, 4)]
    assert (a.word_end, a.unit_end) == (3, 4)


def test_local_align_hueco_en_el_guion_y_en_la_transcripcion() -> None:
    # Unidad del guion sin pareja (el orador se comió "gran").
    a = local_align(["nuevo", "sistema"], ["nuevo", "gran", "sistema"])
    assert a.score == pytest.approx(2.0 - 0.6 + 2.0)
    assert a.pairs == [(0, 0), (1, 2)]
    assert a.max_consecutive_content == 1  # el hueco corta la racha
    # Palabra transcrita sin pareja (una muletilla).
    a = local_align(["nuevo", "este", "sistema"], ["nuevo", "sistema"])
    assert a.score == pytest.approx(3.4)
    assert a.pairs == [(0, 0), (2, 1)]


def test_local_align_sustitucion_cuesta_menos_que_dos_huecos() -> None:
    a = local_align(["nuevo", "gran", "sistema"], ["nuevo", "buen", "sistema"])
    assert a.score == pytest.approx(2.0 - 1.0 + 2.0)  # dos huecos darían 2,8
    assert a.pairs == [(0, 0), (2, 2)]                # la sustitución no es un par
    assert (a.content_matches, a.max_consecutive_content) == (2, 1)


def test_local_align_huecos_largos_no_compensan() -> None:
    # Cinco unidades de por medio cuestan 3,0: más que lo que aporta "sistema".
    a = local_align(["nuevo", "sistema"], ["nuevo", "a", "b", "c", "d", "e", "sistema"])
    assert a.score == pytest.approx(2.0)
    assert a.pairs == [(0, 0)] or a.pairs == [(1, 6)]
    assert len(a.pairs) == 1


def test_local_align_coincidencia_difusa() -> None:
    a = local_align(["presentasion"], ["presentacion"])
    assert a.score == pytest.approx(1.2)
    assert (a.pairs, a.content_matches) == ([(0, 0)], 1)
    # Una inserción o un borrado también son Levenshtein 1.
    assert local_align(["sistemas"], ["sistema"]).score == pytest.approx(1.2)
    assert local_align(["sitema"], ["sistema"]).score == pytest.approx(1.2)
    # Dos ediciones no.
    assert local_align(["sitemas"], ["sistema"]) == Alignment()


def test_local_align_sin_difusa_en_palabras_cortas_ni_stopwords() -> None:
    assert local_align(["sol"], ["sal"]) == Alignment()     # 3 letras: no
    assert local_align(["casa"], ["cosa"]).score == pytest.approx(1.2)  # 4 letras: sí
    assert local_align(["pare"], ["para"]) == Alignment()   # "para" es stopword
    assert local_align(["para"], ["pare"]) == Alignment()


def test_local_align_stopwords_valen_poco() -> None:
    a = local_align(["de", "la"], ["de", "la"])
    assert a.score == pytest.approx(1.0)
    assert (a.content_matches, a.max_consecutive_content) == (0, 0)


def test_local_align_racha_de_contenido_con_stopwords_intercaladas() -> None:
    units = to_units("presentar el nuevo sistema de reconocimiento")
    a = local_align(units, units)
    assert a.content_matches == a.max_consecutive_content == 4
    assert a.score == pytest.approx(4 * 2.0 + 2 * 0.5)


def test_local_align_vacio_y_sin_coincidencias() -> None:
    assert local_align([], ["hola"]) == Alignment()
    assert local_align(["hola"], []) == Alignment()
    a = local_align(["alfa", "bravo"], ["charlie", "delta"])
    assert (a.score, a.pairs, a.word_end, a.unit_end) == (0.0, [], -1, -1)


def test_local_align_empates() -> None:
    # Igual puntaje: gana el que termina antes en las unidades...
    a = local_align(["casa"], ["casa", "perro", "casa"])
    assert a.unit_end == 0
    # ...y el que termina más tarde en las palabras.
    a = local_align(["casa", "perro", "casa"], ["casa"])
    assert (a.word_end, a.unit_end) == (2, 0)


def test_local_align_con_puntajes_propios() -> None:
    scoring = Scoring(content_exact=1.0, stop_exact=1.0, fuzzy=0.5, mismatch=-2.0, gap=-0.1)
    a = local_align(["nuevo", "gran", "sistema"], ["nuevo", "buen", "sistema"], scoring=scoring)
    assert a.score == pytest.approx(1.0 - 0.1 - 0.1 + 1.0)  # ahora conviene el hueco
    assert local_align(["de", "la"], ["de", "la"], scoring=scoring).score == pytest.approx(2.0)


@pytest.mark.parametrize("bad", [
    {"content_exact": 0.0}, {"stop_exact": -0.5}, {"fuzzy": 0},
    {"mismatch": 0.0}, {"gap": 0.6}, {"gap": float("nan")},
])
def test_scoring_valida_signos(bad: dict) -> None:
    with pytest.raises(ValueError):
        Scoring(**bad)


def test_scoring_valida_tipos() -> None:
    with pytest.raises(TypeError):
        Scoring(gap="-0.6")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Scoring(content_exact=True)


# ---------------------------------------------------------------- lectura normal

def test_lectura_normal_con_ventanas_solapadas_sigue_la_cola() -> None:
    for seed in range(20):
        m = ScriptMatcher(TOKENS)
        prev = 0
        for said, r in Lector(m).read(0, TOTAL, random.Random(seed)):
            assert prev <= r.new_idx <= said, (seed, said, r)  # nunca adelante ni atrás
            if said >= 4:
                assert r.new_idx >= said - 1, (seed, said, r)  # sin doble conteo ni atraso
            assert not r.resync and not r.desync
            prev = r.new_idx
        assert m.current_idx == TOTAL


def test_lectura_normal_paso_a_paso() -> None:
    m = _matcher("Hoy vamos a presentar el nuevo sistema de reconocimiento de voz local.")
    r = _say(m, "hoy")
    assert (r.new_idx, r.advanced) == (0, False)      # 2,0 < min_score
    r = _say(m, "hoy vamos a")
    assert (r.new_idx, r.advanced, r.matched_word) == (3, True, "a")
    assert r.confidence == 1.0 and not r.resync and not r.desync
    r = _say(m, "hoy vamos a presentar el nuevo")     # repite y suma tres
    assert (r.new_idx, r.matched_word) == (6, "nuevo")
    r = _say(m, "hoy vamos a presentar el nuevo")     # nada nuevo: en sincronía
    assert (r.new_idx, r.advanced, r.matched_word) == (6, False, "nuevo")
    r = _say(m, "a presentar el nuevo sistema de reconocimiento")
    assert r.new_idx == 9
    r = _say(m, "nuevo sistema de reconocimiento de voz local")
    assert r.new_idx == 12 == m.total_tokens


def test_tildes_mayusculas_y_puntuacion_no_importan() -> None:
    m = _matcher("¡Atención! La CANCIÓN empieza, ¿listos?")
    r = _say(m, "atencion la cancion empieza listos")
    assert r.new_idx == 5 and r.advanced


def test_coincidencia_difusa_avanza() -> None:
    m = _matcher("La presentacion comienza ahora mismo")
    r = _say(m, "la presentasion comienza")
    assert r.new_idx == 3 and r.advanced
    assert r.confidence == pytest.approx((0.5 + 1.2 + 2.0) / 4.5, abs=1e-3)


def test_ejemplo_completo_con_ventanas_aleatorias() -> None:
    tokens = parse_script(EJEMPLO.read_text(encoding="utf-8")).tokens
    for seed in range(30):
        m = ScriptMatcher(tokens)
        lector = Lector(m, tokens, keep=random.Random(seed).randint(6, 12))
        prev = 0
        for said, r in lector.read(0, len(tokens), random.Random(seed)):
            assert prev <= r.new_idx <= said and not r.resync
            prev = r.new_idx
        assert m.current_idx == len(tokens) == 89, seed


# ---------------------------------------------------------------- palabras partidas

def test_palabras_partidas_entre_ventanas() -> None:
    # Hallazgo C3: con bloques de 500 ms sin solapamiento el índice quedaba en
    # 0 y luego saltaba. Con ventanas solapadas la última palabra puede llegar
    # partida, pero la siguiente ventana la trae completa.
    m = _matcher("En la fase dos demostraremos el funcionamiento del módulo de "
                 "reconocimiento de voz.")
    ventanas = [
        "En la fa",
        "En la fase dos demos",
        "En la fase dos demostraremos el funcio",
        "la fase dos demostraremos el funcionamiento del",
        "dos demostraremos el funcionamiento del módulo de recono",
        "el funcionamiento del módulo de reconocimiento de voz.",
    ]
    assert [_say(m, v).new_idx for v in ventanas] == [0, 4, 6, 8, 10, 13]


def test_palabra_partida_al_final_no_cuenta_de_mas() -> None:
    m = _matcher("Hoy vamos a presentar el nuevo sistema de reconocimiento")
    assert _say(m, "hoy vamos a pre").new_idx == 3
    assert _say(m, "hoy vamos a presentar el nue").new_idx == 5
    assert _say(m, "vamos a presentar el nuevo sis").new_idx == 6


# ---------------------------------------------------------------- palabras repetidas

def test_palabras_repetidas_cerca_del_cursor() -> None:
    texto = ("El sistema funciona bien. El sistema funciona rápido. El sistema funciona "
             "siempre. El sistema funciona solo.")
    tokens = parse_script(texto).tokens
    for seed in range(20):
        m = ScriptMatcher(tokens)
        prev = 0
        for said, r in Lector(m, tokens).read(0, len(tokens), random.Random(seed)):
            assert prev <= r.new_idx <= said, (seed, said, r)
            if said >= 4:
                assert r.new_idx >= said - 1, (seed, said, r)
            prev = r.new_idx
        assert m.current_idx == len(tokens)


def test_frase_repetida_sin_contexto_no_salta_a_la_siguiente() -> None:
    m = _matcher("El sistema funciona bien. El sistema funciona rápido. "
                 "El sistema funciona siempre.")
    m.set_position(4)
    # Ambigua: igual podría ser la primera aparición; se queda (conservador).
    assert _say(m, "el sistema funciona").new_idx == 4
    # Con la palabra que la distingue avanza solo hasta ella.
    assert _say(m, "el sistema funciona rapido").new_idx == 8


# ---------------------------------------------------------------- rechazos

@pytest.mark.parametrize("spoken", ["de la", "y que de la", "el", "en el de",
                                    "de la de la de la de la"])
def test_solo_stopwords_no_avanza(spoken: str) -> None:
    m = _matcher("El perro de la casa de la playa y el gato que duerme en el sofá")
    r = _say(m, spoken)
    assert (r.new_idx, r.advanced) == (0, False)
    assert m.current_idx == 0


def test_puntaje_bajo_no_avanza() -> None:
    m = _matcher("Bienvenidos al curso de programación")
    r = _say(m, "bienvenidos señoras señores amigos todos")
    assert (r.new_idx, r.advanced, r.matched_word) == (0, False, None)
    assert 0.0 < r.confidence < 0.5


def test_palabras_fuera_de_la_ventana_no_avanzan() -> None:
    m = ScriptMatcher(TOKENS)
    r = _say(m, "la tarjeta grafica procesa")  # tercer párrafo, lejos de la ventana
    assert (r.new_idx, r.advanced) == (0, False)


def test_cola_que_no_coincide_no_avanza_aunque_la_cabeza_si() -> None:
    m = ScriptMatcher(TOKENS)
    assert _say(m, "buenas tardes a todos y bienvenidos").new_idx == 6
    # Lo último dicho no está en el guion: no se avanza por las palabras viejas.
    r = _say(m, "tardes a todos y bienvenidos eh bueno pues nada")
    assert (r.new_idx, r.advanced) == (6, False)


def test_palabra_suelta_mas_adelante_no_avanza() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(9)
    Lector(m).read(9, 16)
    assert m.current_idx == 16
    # Tras unas palabras ajenas, una palabra que aparece más adelante
    # ("laboratorio", token 28) no basta para saltar hasta ella.
    r = _say(m, "conocer el nuevo sistema camara micro cable laboratorio")
    assert (r.new_idx, r.advanced) == (16, False)


def test_coincidencia_debil_en_la_cola_no_arrastra_el_alineamiento_viejo() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(9)
    Lector(m).read(9, 18)
    assert m.current_idx == 18
    # Lo viejo ("nuevo sistema de reconocimiento") alinea fuerte; lo nuevo
    # ("durante el", tokens 22 y 23) solo se le pega a través de huecos y
    # puntúa menos que lo viejo solo. Avanzar exige ser el óptimo, así que
    # no se avanza hasta que lo nuevo lo justifica por sí mismo.
    ventanas = [
        "conocer el nuevo sistema de reconocimiento camara micro durante el",
        "nuevo sistema de reconocimiento camara micro durante el ultimo",
        "sistema de reconocimiento camara micro durante el ultimo año",
    ]
    assert [_say(m, v).new_idx for v in ventanas] == [18, 18, 26]


def test_alucinacion_cerca_del_cierre_no_avanza_de_mas() -> None:
    # "Gracias por ver" (típica de Whisper ante silencio) cerca de un cierre
    # "muchas gracias por su atención": no se avanza más de lo que justifican
    # las palabras alineadas.
    gracias = TEXTS.index("gracias")
    m = ScriptMatcher(TOKENS)
    m.set_position(P4 - 4)
    Lector(m).read(P4 - 4, P4 + 2)      # "... exacta del lector. Para terminar,"
    assert m.current_idx == P4 + 2
    r = _say(m, "gracias por ver")      # sola: 2,5 < min_score
    assert (r.new_idx, r.advanced) == (P4 + 2, False)
    r = _say(m, "subtitulos realizados por la comunidad de amara org")
    assert (r.new_idx, r.advanced) == (P4 + 2, False)
    r = _say(m, "para terminar gracias por ver")
    assert r.new_idx <= gracias + 2     # a lo sumo hasta "por", nunca "su atención"
    assert m.current_idx < TOTAL
    r = _say(m, "muchas gracias por ver")
    assert m.current_idx <= gracias + 2


def test_vacio_o_sin_guion_no_avanza() -> None:
    m = _matcher("Hola mundo")
    assert m.update([]) == MatchResult(0, False, 0.0, None)
    assert m.update(to_units(f"... {EM_DASH} ?")) == MatchResult(0, False, 0.0, None)
    assert m.update(["", ""]) == MatchResult(0, False, 0.0, None)

    vacio = ScriptMatcher()
    assert vacio.total_tokens == 0
    assert vacio.update(["hola"]) == MatchResult(0, False, 0.0, None)
    assert vacio.set_position(5) == 0
    assert vacio.context_before() == "" and vacio.text_near() == ""


def test_transcripcion_vacia_no_cuenta_como_ciclo() -> None:
    m = ScriptMatcher(TOKENS, stall_cycles=2)
    for _ in range(10):
        assert not m.update([]).desync
    _say(m, "zzz yyy xxx")
    assert not m.desync
    assert m.update([]).desync is False
    assert _say(m, "zzz yyy xxx").desync


# ---------------------------------------------------------------- relecturas

def test_relectura_cercana_no_retrocede_ni_salta() -> None:
    m = ScriptMatcher(TOKENS)
    lector = Lector(m)
    lector.read(0, 20)
    assert m.current_idx == 20
    resultados = []
    for _ in range(3):  # relee "sistema de reconocimiento de voz" tres veces
        for i in range(15, 20):
            resultados.append(lector.say([i]))
    assert len(resultados) > DEFAULT_PARAMS["stall_cycles"]
    assert all(r.new_idx == 20 for r in resultados)
    assert not any(r.resync or r.desync or r.advanced for r in resultados)
    # Y al seguir leyendo avanza desde donde estaba.
    for said, r in lector.read(20, 29):
        assert r.new_idx <= said
    assert m.current_idx == 29


def test_relectura_lejana_solo_retrocede_con_resync_fuerte() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(60)
    lector = Lector(m)
    lector.read(60, 70)
    assert m.current_idx == 70
    back = 40  # 30 palabras atrás: fuera de la ventana local
    resultados = lector.read(back, 60, random.Random(3))
    firsts = [r for _, r in resultados]
    k = next(i for i, r in enumerate(firsts) if r.new_idx != 70)
    # Antes del resync no hay ningún movimiento.
    assert all(not r.advanced for r in firsts[:k])
    jump = firsts[k]
    said_at_jump = resultados[k][0]
    assert jump.resync and not jump.advanced and not jump.desync
    assert said_at_jump - 1 <= jump.new_idx <= said_at_jump
    assert k + 1 <= DEFAULT_PARAMS["stall_cycles"] + 1
    # Después sigue la lectura desde la nueva posición.
    for said, r in resultados[k + 1:]:
        assert said - 1 <= r.new_idx <= said and not r.resync
    assert m.current_idx == 60


def test_relectura_lejana_sin_coincidencia_fuerte_no_retrocede() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(70)
    # Relee de 30 palabras atrás, pero con pocas palabras de contenido seguidas.
    for _ in range(10):
        r = _say(m, "en las pruebas con")
        assert r.new_idx == 70 and not r.resync


# ---------------------------------------------------------------- salto de párrafo

def test_salto_de_parrafo_resincroniza_hacia_adelante() -> None:
    stall = int(DEFAULT_PARAMS["stall_cycles"])
    m = ScriptMatcher(TOKENS)
    lector = Lector(m)
    lector.read(0, P2)
    assert m.current_idx == P2
    assert P3 - P2 > DEFAULT_PARAMS["lookahead"]  # el párrafo 3 queda fuera de la ventana
    # Se salta el segundo párrafo: la primera ventana ya trae tres palabras nuevas.
    resultados = [(P3 + 3, lector.say(range(P3, P3 + 3)))]
    resultados += lector.read(P3 + 3, P4, random.Random(5))
    k = next(i for i, (_, r) in enumerate(resultados) if r.resync)
    assert k + 1 <= stall + 1
    assert all(r.new_idx == P2 and not r.advanced for _, r in resultados[:k])
    said, jump = resultados[k]
    assert jump.advanced and not jump.desync
    assert said - 1 <= jump.new_idx <= said
    for said, r in resultados[k + 1:]:
        assert said - 1 <= r.new_idx <= said
    assert m.current_idx == P4


def test_desync_se_informa_y_se_mantiene_hasta_resync() -> None:
    stall = int(DEFAULT_PARAMS["stall_cycles"])
    m = ScriptMatcher(TOKENS)
    Lector(m).read(0, P2)
    ajeno = "bueno pues ahora miramos otra cosa distinta aqui"
    resultados = [_say(m, ajeno) for _ in range(stall + 3)]
    assert [r.desync for r in resultados] == [False] * (stall - 1) + [True] * 4
    assert all(r.new_idx == P2 and not r.resync for r in resultados)
    assert m.desync
    # Lo que sigue en el párrafo 3 resincroniza y limpia la desincronización.
    r = _say(m, "la tarjeta grafica procesa cada ventana de audio")
    assert r.resync and not r.desync and r.new_idx == P3 + 8
    assert not m.desync


def test_desync_se_limpia_con_un_avance_aceptado() -> None:
    m = ScriptMatcher(TOKENS, stall_cycles=2)
    Lector(m).read(0, 9)
    assert m.current_idx == 9
    assert _say(m, "zzz yyy").desync is False
    assert _say(m, "zzz yyy").desync is True
    r = _say(m, "presentacion hoy vamos a conocer")
    assert (r.advanced, r.desync, r.resync) == (True, False, False)
    assert r.new_idx == 13


def test_set_position_y_load_limpian_desync() -> None:
    m = ScriptMatcher(TOKENS, stall_cycles=1)
    assert _say(m, "zzz").desync
    m.set_position(3)
    assert not m.desync
    assert _say(m, "zzz").desync
    m.load(TOKENS)
    assert not m.desync and m.current_idx == 0
    # El contador también se reinicia: con stall_cycles=2 hace falta fallar dos veces.
    m2 = ScriptMatcher(TOKENS, stall_cycles=2)
    assert not _say(m2, "zzz").desync
    m2.set_position(0)
    assert not _say(m2, "zzz").desync
    assert _say(m2, "zzz").desync


def test_ruido_prolongado_nunca_mueve_el_indice() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(30)
    rnd = random.Random(7)
    ruido = ["eh", "bueno", "mmm", "vale", "este", "o", "sea", "pues", "nada", "ya", "si", "no"]
    for _ in range(60):
        r = m.update([rnd.choice(ruido) for _ in range(rnd.randint(1, 8))])
        assert r.new_idx == 30 and not r.advanced and not r.resync


# ---------------------------------------------------------------- fin del guion

def test_fin_del_guion_llega_al_total_y_se_queda() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(P3)
    lector = Lector(m)
    lector.read(P3, TOTAL)
    assert m.current_idx == TOTAL
    for _ in range(10):
        r = _say(m, "para terminar muchas gracias por su atencion")
        assert (r.new_idx, r.advanced, r.resync, r.desync) == (TOTAL, False, False, False)
    for _ in range(10):
        r = _say(m, "y ahora pasamos a las preguntas del publico")
        assert r.new_idx == TOTAL and not r.resync


def test_signos_al_final_del_guion_se_saltan_hasta_el_total() -> None:
    m = _matcher(f"Gracias por venir hoy {EM_DASH} ...")
    r = _say(m, "gracias por venir hoy")
    assert r.new_idx == m.total_tokens == 6


# ---------------------------------------------------------------- unidades

def test_tokens_sin_unidades_se_saltan() -> None:
    text = f"Hola {EM_DASH} mundo ... genial ¿ ? fin del ensayo"
    tokens = parse_script(text).tokens
    assert [t.units for t in tokens][1] == [] and tokens[3].units == []
    m = ScriptMatcher(tokens)
    r = _say(m, "hola mundo")
    assert r.new_idx == 4                  # salta "..."
    assert tokens[r.new_idx].text == "genial"
    r = _say(m, "hola mundo genial")
    assert r.new_idx == 7                  # salta "¿" y "?"
    assert tokens[r.new_idx].text == "fin"
    r = _say(m, "mundo genial fin del ensayo")
    assert r.new_idx == 10 == m.total_tokens


def test_set_position_en_token_sin_unidades_no_avanza_solo() -> None:
    m = _matcher(f"Hola amigo {EM_DASH} mundo feliz")
    m.set_position(2)
    r = _say(m, "hola amigo")  # relee lo anterior: en sincronía, sin avance
    assert (r.new_idx, r.advanced) == (2, False)
    r = _say(m, "hola amigo mundo feliz")
    assert r.new_idx == 5


@pytest.mark.parametrize("spoken", [
    "la nueva tarjeta RTX 5070",
    "la nueva tarjeta rtx cinco mil setenta",
    "nueva tarjeta cinco mil setenta",
])
def test_digitos_y_numeros_en_palabras(spoken: str) -> None:
    m = _matcher("La nueva tarjeta RTX-5070 es muy rápida")
    r = _say(m, spoken)
    assert (r.new_idx, r.advanced, r.matched_word) == (4, True, "RTX-5070")


def test_numeros_en_palabras_en_el_guion_y_digitos_en_la_transcripcion() -> None:
    m = _matcher("Con la tarjeta cinco mil setenta todo va mejor")
    r = _say(m, "con la tarjeta 5070")
    assert r.new_idx == 6 and r.matched_word == "setenta"


def test_palabras_con_barra_y_guion() -> None:
    m = _matcher("Elige uno/dos y manda un e-mail ahora mismo")
    r = _say(m, "elige uno dos y manda")
    assert (r.new_idx, r.matched_word) == (4, "manda")
    r = _say(m, "y manda un e mail ahora")
    assert (r.new_idx, r.matched_word) == (7, "ahora")


# ---------------------------------------------------------------- monotonía y topes

def test_tope_de_avance_por_llamada() -> None:
    texto = ("ana bruno carla diego elena fabio gloria hugo irene julio "
             "karen lucas marta nicolas olga pablo quique rosa sergio teresa")
    m = _matcher(texto)
    r = _say(m, "irene julio karen")      # tokens 8 a 10: alinea lejos
    assert (r.new_idx, r.advanced) == (DEFAULT_PARAMS["max_advance"], True) == (8, True)
    r = _say(m, "irene julio karen")      # la siguiente llamada completa el avance
    assert r.new_idx == 11
    m = _matcher(texto, max_advance=3)
    assert _say(m, "irene julio karen").new_idx == 3


def test_set_position_recorta_y_devuelve_el_valor_aplicado() -> None:
    m = _matcher("uno dos tres cuatro cinco")
    assert m.set_position(-5) == 0 and m.current_idx == 0
    assert m.set_position(999) == 5 and m.current_idx == 5
    assert m.set_position(3) == 3 and m.current_idx == 3


def test_set_position_permite_retroceder_a_mano() -> None:
    m = ScriptMatcher(TOKENS)
    Lector(m).read(0, 20)
    assert m.set_position(9) == 9
    r = _say(m, "hoy vamos a conocer")
    assert r.new_idx == 13


def test_current_idx_y_tokens_son_de_solo_lectura() -> None:
    m = _matcher("hola mundo")
    with pytest.raises(AttributeError):
        m.current_idx = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        m.tokens = []  # type: ignore[misc]
    assert isinstance(m.tokens, tuple)
    assert [t.text for t in m.tokens] == ["hola", "mundo"]


def test_load_reinicia_la_posicion() -> None:
    m = ScriptMatcher(TOKENS)
    Lector(m).read(0, 9)
    assert m.current_idx == 9
    m.load(parse_script("otro guion distinto").tokens)
    assert (m.current_idx, m.total_tokens) == (0, 3)


def test_acepta_texto_crudo_por_comodidad() -> None:
    m = _matcher("La tarjeta RTX-5070 es muy rápida")
    r = m.update("La tarjeta RTX 5070")  # type: ignore[arg-type]
    assert r.new_idx == 3


def test_transcripcion_muy_larga_se_recorta_a_la_cola() -> None:
    m = ScriptMatcher(TOKENS)
    m.set_position(60)
    words = ["zzz"] * (MAX_WORDS * 3) + to_units(" ".join(TEXTS[60:66]))
    r = m.update(words)
    assert r.new_idx == 66


# ---------------------------------------------------------------- contexto para el STT y el filtro

def test_context_before_y_text_near() -> None:
    m = ScriptMatcher(TOKENS)
    assert m.context_before() == ""
    m.set_position(P2)
    assert m.context_before(3) == "en nuestro laboratorio."
    assert m.context_before(0) == ""
    assert m.context_before(1000) == " ".join(TEXTS[:P2])
    assert m.text_near(2, 3) == "nuestro laboratorio. Primero revisaremos la"
    assert m.text_near() == " ".join(TEXTS[P2 - 10:P2 + 50])
    m.set_position(TOTAL)
    assert m.text_near(1, 5) == "atención."


# ---------------------------------------------------------------- parámetros

def test_parametros_por_defecto() -> None:
    m = ScriptMatcher()
    assert m.params == {
        "lookbehind": 10, "lookahead": 25, "min_score": 3.0, "max_advance": 8,
        "tail_words": 3, "stall_cycles": 6, "resync_lookbehind": 40,
        "resync_lookahead": 200, "resync_min_consecutive": 4,
        "content_exact": 2.0, "stop_exact": 0.5, "fuzzy": 1.2, "mismatch": -1.0, "gap": -0.6,
    }
    assert m.scoring == Scoring()


def test_parametros_por_nombre() -> None:
    m = ScriptMatcher(TOKENS, lookahead=40, min_score=4, content_exact=3.0, gap=-1)
    assert m.params["lookahead"] == 40 and isinstance(m.params["lookahead"], int)
    assert m.params["min_score"] == 4.0 and isinstance(m.params["min_score"], float)
    assert m.scoring == Scoring(content_exact=3.0, gap=-1.0)
    assert ScriptMatcher(stall_cycles=3.0).params["stall_cycles"] == 3


@pytest.mark.parametrize("bad", [{"lookahed": 20}, {"scoring": Scoring()}, {"sticky_updates": 1}])
def test_parametros_desconocidos_fallan(bad: dict) -> None:
    with pytest.raises(TypeError):
        ScriptMatcher(None, **bad)


@pytest.mark.parametrize("bad", [{"lookahead": "25"}, {"min_score": None}, {"max_advance": True}])
def test_parametros_de_tipo_invalido_fallan(bad: dict) -> None:
    with pytest.raises(TypeError):
        ScriptMatcher(None, **bad)


@pytest.mark.parametrize("bad", [
    {"lookahead": 0}, {"lookbehind": -1}, {"max_advance": 0}, {"tail_words": 0},
    {"stall_cycles": 0}, {"resync_min_consecutive": 0}, {"lookahead": 2.5},
    {"min_score": -1.0}, {"min_score": float("inf")}, {"gap": 0.0}, {"fuzzy": -1.2},
])
def test_parametros_fuera_de_rango_fallan(bad: dict) -> None:
    with pytest.raises(ValueError):
        ScriptMatcher(None, **bad)


def test_parametros_cambian_el_comportamiento() -> None:
    # Con min_score bajo, dos palabras de contenido sueltas bastan.
    m = ScriptMatcher(TOKENS, min_score=2.0)
    assert _say(m, "buenas").new_idx == 1
    # Con tail_words=1 la última palabra debe estar alineada.
    m = _matcher("Hoy vamos a presentar el nuevo sistema", tail_words=1)
    assert _say(m, "hoy vamos a presentar zzz").new_idx == 0
    m = _matcher("Hoy vamos a presentar el nuevo sistema")
    assert _say(m, "hoy vamos a presentar zzz").new_idx == 4


# ---------------------------------------------------------------- rendimiento

def test_rendimiento() -> None:
    tokens = TOKENS * 6
    words = to_units(" ".join(TEXTS[52:62]))
    m = ScriptMatcher(tokens)
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        m.set_position(60)
        m.update(words)
    typical_ms = (time.perf_counter() - t0) / n * 1000
    # Resync: falla la ventana local y busca en ~240 tokens.
    m = ScriptMatcher(tokens, stall_cycles=1)
    far = to_units(" ".join(TEXTS[P3:P3 + 10]))
    t0 = time.perf_counter()
    for _ in range(20):
        m.set_position(P3 + 2 * TOTAL - 30)
        r = m.update(far)
        assert r.resync
    resync_ms = (time.perf_counter() - t0) / 20 * 1000
    # Holgura de sobra respecto a lo medido (< 0,2 ms y < 2 ms en la máquina de desarrollo).
    assert typical_ms < 2.0, typical_ms
    assert resync_ms < 30.0, resync_ms
