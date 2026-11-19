"""Filtro de alucinaciones de Whisper (docs/diseno_v2.md, secciones 2 y 8; Fase 2, paso 3).

Las transcripciones se construyen a mano con los tipos del motor real y del
falso: no se carga ningún modelo.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import fakes
from tools.stt_engine import SegmentInfo, Transcription
from tools.text_norm import is_stopword, to_units
from tools.transcript_filter import (
    BLACKLIST_ES,
    REASON_BLACKLIST,
    REASON_COMPRESSION,
    REASON_EMPTY,
    REASON_NO_SPEECH,
    REASON_STOPWORD,
    FilterOutcome,
    clean_transcription,
)


def _seg(text: str, no_speech: float = 0.05, logprob: float = -0.3,
         ratio: float = 1.3) -> SegmentInfo:
    return SegmentInfo(text=text, no_speech_prob=no_speech, avg_logprob=logprob,
                       compression_ratio=ratio)


def _tr(*segments: SegmentInfo) -> Transcription:
    return Transcription(text=" ".join(s.text for s in segments if s.text),
                         segments=list(segments))


def _say(text: str, **metrics: float) -> Transcription:
    """Transcripción de un solo segmento con métricas de voz clara."""
    return _tr(_seg(text, **metrics))


# ---------------------------------------------------------------- voz normal

def test_conserva_voz_normal_sin_tocar_el_texto() -> None:
    out = clean_transcription(_say("Hoy les presento la RTX-5070, ¿sí?"))
    assert out == FilterOutcome("Hoy les presento la RTX-5070, ¿sí?", None,
                                "Hoy les presento la RTX-5070, ¿sí?")
    assert out.kept


def test_una_sola_palabra_de_contenido_se_conserva() -> None:
    assert clean_transcription(_say("Presupuesto.")).reason is None
    assert clean_transcription(_say("No.")).reason is None  # "no" no es stopword


def test_solo_stopwords_pero_mas_de_una_se_conserva() -> None:
    # Dos palabras funcionales no son "una sola stopword": decide el matcher.
    assert clean_transcription(_say("de la")).text == "de la"


# ---------------------------------------------------------------- segmentos

def test_descarta_segmento_sin_voz_y_con_baja_confianza() -> None:
    out = clean_transcription(_say("Hola a todos", no_speech=0.9, logprob=-1.5))
    assert out == FilterOutcome("", REASON_NO_SPEECH, "Hola a todos")
    assert not out.kept


@pytest.mark.parametrize("no_speech,logprob", [
    (0.9, -0.5),   # Whisper duda de que haya voz, pero el texto es confiable
    (0.2, -1.5),   # texto poco confiable, pero hubo voz
    (0.6, -1.5),   # en el umbral exacto no se descarta (comparación estricta)
    (0.9, -1.0),
])
def test_sin_voz_requiere_ambas_condiciones(no_speech: float, logprob: float) -> None:
    out = clean_transcription(_say("Hola a todos", no_speech=no_speech, logprob=logprob))
    assert out.reason is None
    assert out.text == "Hola a todos"


def test_descarta_segmento_repetitivo_por_compresion() -> None:
    text = "y entonces y entonces y entonces y entonces y entonces"
    out = clean_transcription(_say(text, ratio=3.1))
    assert out == FilterOutcome("", REASON_COMPRESSION, text)
    assert clean_transcription(_say("hola mundo", ratio=2.4)).reason is None  # umbral estricto


def test_umbrales_configurables() -> None:
    tr = _say("Hola a todos", no_speech=0.5, logprob=-0.8, ratio=2.0)
    assert clean_transcription(tr).reason is None
    assert clean_transcription(tr, no_speech_threshold=0.4,
                               logprob_threshold=-0.5).reason == REASON_NO_SPEECH
    assert clean_transcription(tr, compression_threshold=1.8).reason == REASON_COMPRESSION


def test_descarta_solo_los_segmentos_malos() -> None:
    tr = _tr(_seg("Bienvenidos a la demo."),
             _seg("Subtítulos realizados por la comunidad de Amara.org",
                  no_speech=0.8, logprob=-1.4),
             _seg("Empecemos.", ratio=2.0))
    out = clean_transcription(tr)
    assert out.reason is None
    assert out.text == "Bienvenidos a la demo. Empecemos."
    assert "Amara" in out.original


def test_todos_los_segmentos_descartados_informan_el_primer_motivo() -> None:
    tr = _tr(_seg("bla bla bla bla", ratio=3.0), _seg("hola", no_speech=0.95, logprob=-2.0))
    assert clean_transcription(tr).reason == REASON_COMPRESSION


# ---------------------------------------------------------------- lista negra

@pytest.mark.parametrize("text", [
    "Subtítulos realizados por la comunidad de Amara.org",
    "SUBTITULOS REALIZADOS POR LA COMUNIDAD DE AMARA.ORG",
    "Subtitulado por la comunidad de Amara.org",
    "Subtítulos por la comunidad de Amara.org",
    "Subtítulos en español",
    "¡Gracias por ver!",
    "Gracias por ver el vídeo.",
    "Gracias por mirar.",
    "Gracias.",
    "¡Suscríbete!",
    "Suscríbete al canal.",
    "No olvides suscribirte.",
    "Dale like",
    "[Música]",
    "(Aplausos)",
    "*Risas*",
    "www.amara.org",
])
def test_lista_negra_descarta_alucinaciones(text: str) -> None:
    out = clean_transcription(_say(text))
    assert out == FilterOutcome("", REASON_BLACKLIST, text)


def test_lista_negra_repetida_o_combinada_se_descarta() -> None:
    assert clean_transcription(_say("Gracias por ver. Gracias por ver. Gracias por ver.")
                               ).reason == REASON_BLACKLIST
    assert clean_transcription(_say("¡Gracias por ver! ¡Suscríbete!")).reason == REASON_BLACKLIST
    # Unidas por stopwords sueltas también son solo alucinación.
    assert clean_transcription(_say("Gracias por ver y suscríbete al canal")
                               ).reason == REASON_BLACKLIST


def test_lista_negra_en_varios_segmentos_se_descarta() -> None:
    tr = _tr(_seg("Gracias por ver."), _seg("Subtítulos realizados por la comunidad de Amara.org"))
    assert clean_transcription(tr).reason == REASON_BLACKLIST


def test_lista_negra_se_conserva_si_el_guion_cercano_la_contiene() -> None:
    near = "y con esto termina la demostración. ¡Gracias por ver! Nos vemos"
    out = clean_transcription(_say("Gracias por ver."), near_text=near)
    assert out == FilterOutcome("Gracias por ver.", None, "Gracias por ver.")
    # Plegado igual que el guion: tildes y mayúsculas no importan.
    out = clean_transcription(_say("[Música]"), near_text="Ahora suena la MÚSICA de fondo")
    assert out.reason is None


def test_lista_negra_cercana_debe_ser_la_misma_frase_contigua() -> None:
    # Cierre "muchas gracias" en el guion: la alucinación "gracias por ver" no lo usa.
    near = "Eso es todo por hoy. Muchas gracias a todos."
    assert clean_transcription(_say("¡Gracias por ver!"), near_text=near
                               ).reason == REASON_BLACKLIST
    # Las palabras sueltas en otro orden no cuentan como la frase.
    assert clean_transcription(_say("Suscríbete al canal"), near_text="el canal para suscribete"
                               ).reason == REASON_BLACKLIST
    # Pero el "muchas gracias" del cierre sí se conserva.
    assert clean_transcription(_say("Muchas gracias."), near_text=near).reason is None


def test_frase_del_guion_mas_otra_alucinacion_se_conserva() -> None:
    # Parte de lo dicho está en el guion: el matcher decide con la parte real.
    near = "Para terminar, gracias por ver esta presentación"
    out = clean_transcription(_say("Gracias por ver. Suscríbete."), near_text=near)
    assert out.reason is None


def test_voz_real_mezclada_con_lista_negra_se_conserva() -> None:
    text = "Hoy hablaremos del presupuesto. Subtítulos realizados por la comunidad de Amara.org"
    out = clean_transcription(_say(text))
    assert out == FilterOutcome(text, None, text)
    assert clean_transcription(_say("Muchas gracias a todos")).reason is None
    assert clean_transcription(_say("la música clásica")).reason is None


# ---------------------------------------------------------------- stopwords

@pytest.mark.parametrize("text", ["y", "De.", "  la  ", "¿Que?", "Y..."])
def test_una_sola_stopword_se_descarta(text: str) -> None:
    out = clean_transcription(_say(text))
    assert out.reason == REASON_STOPWORD
    assert out.text == ""


# ---------------------------------------------------------------- vacíos y formas

@pytest.mark.parametrize("tr", [
    Transcription(text="", segments=[]),
    Transcription(text="", segments=[_seg(""), _seg("   ")]),
    Transcription(text="...", segments=[_seg("...")]),
    Transcription(text="¿?", segments=[]),
    SimpleNamespace(text=None, segments=None),
    SimpleNamespace(),
])
def test_transcripcion_vacia(tr: object) -> None:
    out = clean_transcription(tr)
    assert out.text == ""
    assert out.reason == REASON_EMPTY


def test_sin_segmentos_filtra_el_texto() -> None:
    assert clean_transcription(SimpleNamespace(text="  Hola   mundo ", segments=[])
                               ) == FilterOutcome("Hola mundo", None, "Hola mundo")
    assert clean_transcription(SimpleNamespace(text="¡Suscríbete!", segments=[])
                               ).reason == REASON_BLACKLIST


def test_acepta_la_transcripcion_del_stt_falso() -> None:
    # backend.fakes.FakeSTTEngine devuelve un segmento con métricas de voz clara.
    tr = fakes.Transcription("Gracias por ver", [fakes.SegmentInfo("Gracias por ver")])
    assert clean_transcription(tr).reason == REASON_BLACKLIST
    tr = fakes.Transcription("hola mundo", [fakes.SegmentInfo("hola mundo")])
    assert clean_transcription(tr) == FilterOutcome("hola mundo", None, "hola mundo")


def test_segmentos_sin_metricas_no_se_descartan() -> None:
    tr = SimpleNamespace(text="", segments=[SimpleNamespace(text="hola mundo")])
    assert clean_transcription(tr) == FilterOutcome("hola mundo", None, "hola mundo")


# ---------------------------------------------------------------- BLACKLIST_ES

@pytest.mark.parametrize("phrase", [
    "subtitulos realizados por la comunidad de amara org",
    "subtitulado por la comunidad de amara org",
    "subtitulos por la comunidad de amara org",
    "subtitulos en espanol",
    "gracias por ver",
    "gracias por ver el video",
    "gracias por mirar",
    "suscribete",
    "suscribete al canal",
    "no olvides suscribirte",
    "dale like",
    "musica",
    "aplausos",
    "risas",
    "amara org",
    "www",
])
def test_blacklist_incluye_las_frases_del_plan(phrase: str) -> None:
    assert tuple(phrase.split()) in BLACKLIST_ES


def test_blacklist_normalizada_y_sin_repetidos() -> None:
    assert BLACKLIST_ES
    assert len(set(BLACKLIST_ES)) == len(BLACKLIST_ES)
    for phrase in BLACKLIST_ES:
        assert isinstance(phrase, tuple) and phrase
        assert list(phrase) == to_units(" ".join(phrase))
        # Toda frase tiene al menos una palabra de contenido: si no, las
        # stopwords que unen frases bastarían para descartar cualquier cosa.
        assert any(not is_stopword(u) for u in phrase)
