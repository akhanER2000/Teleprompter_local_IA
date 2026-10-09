"""Matcher de la Fase 1: algoritmo voraz por unidades (docs/diseno_v2.md, sección 5)."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from tools.script_matcher import DEFAULT_PARAMS, MatchResult, ScriptMatcher
from tools.script_parser import parse_script
from tools.text_norm import to_units

EM_DASH = "\u2014"


def _matcher(text: str, **params) -> ScriptMatcher:
    return ScriptMatcher(parse_script(text).tokens, **params)


def _say(m: ScriptMatcher, text: str) -> MatchResult:
    return m.update(to_units(text))


# ---------------------------------------------------------------- lectura

def test_lectura_normal_avanza_frase_a_frase() -> None:
    m = _matcher("Hoy vamos a presentar el nuevo sistema de reconocimiento de voz local.")
    assert m.current_idx == 0

    r = _say(m, "Hoy vamos a")
    assert (r.new_idx, r.advanced, r.matched_word) == (3, True, "a")
    assert r.confidence == 1.0 and not r.resync and not r.desync

    r = _say(m, "presentar el nuevo")
    assert (r.new_idx, r.advanced, r.matched_word) == (6, True, "nuevo")

    r = _say(m, "sistema de reconocimiento")
    assert r.new_idx == 9
    r = _say(m, "de voz local")
    assert r.new_idx == 12 == m.total_tokens
    assert m.current_idx == 12

    # Al final del guion no hay nada más que avanzar.
    r = _say(m, "local")
    assert (r.new_idx, r.advanced) == (12, False)


def test_tildes_mayusculas_y_puntuacion_no_importan() -> None:
    m = _matcher("¡Atención! La CANCIÓN empieza, ¿listos?")
    r = _say(m, "atencion la cancion empieza listos")
    assert r.new_idx == 5 and r.advanced


def test_coincidencia_difusa_de_una_letra() -> None:
    m = _matcher("La presentacion comienza ahora mismo")
    r = _say(m, "la presentasion comienza")  # una sustitución
    assert r.new_idx == 3 and r.advanced
    assert r.confidence == pytest.approx((1 + 0.7 + 1) / 3, abs=1e-3)


# ---------------------------------------------------------------- unidades

@pytest.mark.parametrize("spoken", ["RTX 5070", "rtx cinco mil setenta", "cinco mil setenta"])
def test_rtx_5070_en_digitos_o_palabras(spoken: str) -> None:
    m = _matcher("La tarjeta RTX-5070 es muy rápida")
    r = _say(m, "la tarjeta")
    assert r.new_idx == 2
    r = _say(m, spoken)
    assert (r.new_idx, r.advanced, r.matched_word) == (3, True, "RTX-5070")


def test_rtx_5070_tras_set_position() -> None:
    m = _matcher("La tarjeta RTX-5070 es muy rápida")
    assert m.set_position(2) == 2
    r = _say(m, "rtx cinco mil setenta")
    assert (r.new_idx, r.matched_word) == (3, "RTX-5070")


def test_palabras_con_barra_y_guion() -> None:
    m = _matcher("Elige uno/dos y manda un e-mail")
    r = _say(m, "elige uno")
    assert (r.new_idx, r.matched_word) == (2, "uno/dos")
    r = _say(m, "y manda un e mail")
    assert (r.new_idx, r.matched_word) == (6, "e-mail")


def test_email_junto_coincide_de_forma_difusa_con_mail() -> None:
    m = _matcher("Elige uno/dos y manda un e-mail")
    m.set_position(4)
    r = _say(m, "un email")  # "email" contra la unidad "mail": Levenshtein 1
    assert r.new_idx == 6


def test_tokens_sin_unidades_se_saltan() -> None:
    text = f"Hola {EM_DASH} mundo ... genial ¿ ? fin"
    tokens = parse_script(text).tokens
    assert [t.units for t in tokens][1] == []
    m = ScriptMatcher(tokens)

    r = _say(m, "hola")
    assert r.new_idx == 2  # salta el guion largo
    assert tokens[r.new_idx].text == "mundo"

    r = _say(m, "mundo")
    assert r.new_idx == 4  # salta "..."
    r = _say(m, "genial")
    assert r.new_idx == 7  # salta "¿" y "?"
    assert tokens[r.new_idx].text == "fin"
    r = _say(m, "fin")
    assert r.new_idx == 8 == m.total_tokens


def test_signos_al_final_del_guion_se_saltan_hasta_el_total() -> None:
    m = _matcher(f"Gracias por venir {EM_DASH} ...")
    r = _say(m, "gracias por venir")
    assert r.new_idx == m.total_tokens == 5


def test_set_position_en_token_sin_unidades_no_avanza_solo() -> None:
    m = _matcher(f"Hola {EM_DASH} mundo")
    m.set_position(1)
    r = _say(m, "hola")  # relee lo anterior: no hay avance real
    assert (r.new_idx, r.advanced) == (1, False)
    r = _say(m, "mundo")
    assert r.new_idx == 3


# ---------------------------------------------------------------- rechazos

_FRUTAS = ("manzana pera uva melon sandia kiwi mango papaya fresa cereza "
           "limon naranja ciruela durazno higo granada coco pina guayaba mora "
           "frambuesa arandano lima pomelo nispero membrillo caqui datil tamarindo maracuya "
           "lichi carambola mandarina pitaya chirimoya lucuma tuna zapote mamey grosella")

@pytest.mark.parametrize("spoken", ["de la", "y que de la", "el", "en el de"])
def test_solo_stopwords_no_avanza(spoken: str) -> None:
    m = _matcher("El perro de la casa de la playa y el gato que duerme en el sofá")
    r = _say(m, spoken)
    assert (r.new_idx, r.advanced) == (0, False)
    assert m.current_idx == 0


def test_palabra_corta_no_ancla() -> None:
    # "va" no es stopword pero tiene menos de 3 letras: no sirve de ancla.
    m = _matcher("Va a la casa del pueblo")
    r = _say(m, "va a la")
    assert (r.new_idx, r.advanced) == (0, False)


def test_confianza_baja_no_avanza() -> None:
    m = _matcher("Bienvenidos al curso de programación")
    r = _say(m, "bienvenidos señoras señores amigos todos")
    assert r.confidence == pytest.approx(0.2)
    assert (r.new_idx, r.advanced) == (0, False)
    assert r.matched_word == "Bienvenidos"  # informativo aunque no avance


def test_palabras_fuera_de_la_ventana_no_avanzan() -> None:
    m = _matcher(_FRUTAS)
    assert m.total_tokens == 40
    r = _say(m, "tamarindo maracuya")  # tokens 28 y 29, lejos de la ventana
    assert (r.new_idx, r.advanced, r.confidence) == (0, False, 0.0)


def test_vacio_o_sin_guion_no_avanza() -> None:
    m = _matcher("Hola mundo")
    assert m.update([]) == MatchResult(0, False, 0.0, None)
    assert m.update(to_units(f"... {EM_DASH} ?")) == MatchResult(0, False, 0.0, None)

    vacio = ScriptMatcher()
    assert vacio.total_tokens == 0
    assert vacio.update(["hola"]) == MatchResult(0, False, 0.0, None)
    assert vacio.set_position(5) == 0


# ---------------------------------------------------------------- monotonía y topes

def test_monotono_no_retrocede_al_releer() -> None:
    m = _matcher("alfa bravo charlie delta eco foxtrot golf hotel india juliet")
    assert _say(m, "alfa bravo charlie delta").new_idx == 4
    for spoken in ("charlie delta", "bravo", "alfa bravo charlie"):
        r = _say(m, spoken)
        assert (r.new_idx, r.advanced) == (4, False)
    assert _say(m, "eco foxtrot").new_idx == 6


def test_set_position_recorta_y_devuelve_el_valor_aplicado() -> None:
    m = _matcher("uno dos tres cuatro cinco")
    assert m.set_position(-5) == 0 and m.current_idx == 0
    assert m.set_position(999) == 5 and m.current_idx == 5
    assert m.set_position(3) == 3 and m.current_idx == 3
    assert m.set_position(5) == 5


def test_current_idx_es_de_solo_lectura() -> None:
    m = _matcher("hola mundo")
    with pytest.raises(AttributeError):
        m.current_idx = 1  # type: ignore[misc]


def test_set_position_permite_retroceder_a_mano() -> None:
    m = _matcher("alfa bravo charlie delta eco")
    assert _say(m, "alfa bravo charlie").new_idx == 3
    assert m.set_position(1) == 1
    r = _say(m, "bravo")
    assert r.new_idx == 2


def test_load_reinicia_la_posicion() -> None:
    m = _matcher("alfa bravo charlie")
    _say(m, "alfa bravo")
    assert m.current_idx == 2
    m.load(parse_script("otro guion distinto").tokens)
    assert (m.current_idx, m.total_tokens) == (0, 3)


_VEINTE = ("ana bruno carla diego elena fabio gloria hugo irene julio "
           "karen lucas marta nicolas olga pablo quique rosa sergio teresa")


def test_tope_de_avance_por_llamada() -> None:
    # Pocas palabras que alinean lejos: el tope limita el salto.
    m = _matcher(_VEINTE)
    r = _say(m, "ana julio")
    assert (r.new_idx, r.advanced) == (DEFAULT_PARAMS["max_advance"], True) == (6, True)


def test_tope_configurable_por_params() -> None:
    m = _matcher(_VEINTE, max_advance=8)
    r = _say(m, "ana julio")
    assert r.new_idx == 8


def test_bloque_largo_avance_completo() -> None:
    # Con inferencia lenta (CPU) cada bloque trae muchas palabras: el tope y la
    # ventana crecen con el bloque, si no el matcher se queda atrás para siempre.
    m = _matcher(_VEINTE)
    r = _say(m, "ana bruno carla diego elena fabio gloria hugo irene julio")
    assert (r.new_idx, r.advanced) == (10, True)
    r = _say(m, "karen lucas marta nicolas olga pablo quique rosa sergio teresa")
    assert r.new_idx == 20 == m.total_tokens


def test_sticky_desactivado_por_defecto() -> None:
    assert DEFAULT_PARAMS["sticky_updates"] == 0
    m = _matcher(_VEINTE)
    m.set_position(0)
    assert _say(m, "ana bruno carla diego elena").new_idx == 5


def test_ventana_estrecha_tras_set_position_si_se_activa() -> None:
    m = _matcher(_VEINTE, sticky_updates=4)
    m.set_position(0)
    # Una palabra fuera de la ventana estrecha (3 tokens, o el bloque + 2) no se alcanza.
    assert _say(m, "fabio gloria").new_idx == 0
    # La ventana estrecha nunca es menor que el bloque: un bloque de 4 avanza 4.
    assert _say(m, "ana bruno carla diego").new_idx == 4
    # Tras `sticky_updates` llamadas vuelve la ventana normal.
    m.update(["zzz"])
    m.update(["zzz"])
    assert _say(m, "fabio gloria hugo").new_idx == 8


EJEMPLO = Path(__file__).resolve().parent.parent / "guiones" / "ejemplo.txt"


def _leer_en_bloques(sizes) -> int:
    """Lee guiones/ejemplo.txt tras set_position(0) en bloques de `sizes` tokens."""
    tokens = parse_script(EJEMPLO.read_text(encoding="utf-8")).tokens
    m = ScriptMatcher(tokens)
    m.set_position(0)  # como cada `start`
    i = 0
    for k in sizes:
        if i >= len(tokens):
            break
        chunk = tokens[i:i + k]
        i += k
        if chunk:
            m.update(to_units(" ".join(t.text for t in chunk)))
    return m.current_idx


@pytest.mark.parametrize("k", range(1, 13))
def test_ejemplo_completo_con_bloques_de_k_palabras(k: int) -> None:
    # Con el modo sticky y bloques de 4 o más palabras se detenía en 0 a 6 de 89.
    assert _leer_en_bloques([k] * 200) == 89


def test_ejemplo_completo_con_bloques_aleatorios() -> None:
    # Bloques de 0 a 10 palabras, como los de una inferencia con latencia variable.
    for seed in range(60):
        rnd = random.Random(seed)
        sizes = [rnd.randint(0, 10) for _ in range(400)]
        assert _leer_en_bloques(sizes) == 89, seed


def test_parametros_desconocidos_fallan() -> None:
    with pytest.raises(TypeError):
        ScriptMatcher(None, lookahed=20)


def test_acepta_texto_crudo_por_comodidad() -> None:
    m = _matcher("La tarjeta RTX-5070 es muy rápida")
    r = m.update("La tarjeta RTX 5070")
    assert r.new_idx == 3
