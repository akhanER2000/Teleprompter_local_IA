"""Hito 85 (R2-R5): referencias del guion: parser, voz, resolución, miniaturas, caché y /control.

Mide lo visible desde fuera: los segmentos que recibe el visor, los tokens que oye el
matcher, lo que responde GET /api/ref (bytes del PNG, cabeceras, «No encontrado») y lo que
valida el contrato de control. Sin red: la única conversión externa es LibreOffice o
PowerPoint locales, y esa prueba se salta (y lo dice) si no hay ninguno.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import server as server_mod
from backend.config import Settings
from backend.protocol import (
    ESTADO_FIELDS, ProtocolError, estado_control_msg, parse_control_message, sanitize_estado,
)
from tools import ref_thumbs, slides_engine
from tools.script_matcher import ScriptMatcher
from tools.script_parser import parse_script
from tools.text_norm import to_units

from test_parser import check_invariants

ROOT = Path(__file__).resolve().parent.parent
EJEMPLO = ROOT / "guiones" / "ejemplo_referencias.txt"
RECURSOS = ROOT / "guiones" / "ejemplo_referencias_recursos"


def refs(text: str) -> list[dict]:
    p = parse_script(text)
    check_invariants(p)
    return [s for s in p.segments if s["type"] == "ref"]


# ---------------------------------------------------------------- parser: cada tipo

@pytest.mark.parametrize("text, kind, file, extra", [
    ("[img: portada.png]", "img", "portada.png", {}),
    ("[ppt: informe.pptx#5]", "ppt", "informe.pptx", {"page": 5, "frag": "5"}),
    ("[pdf: informe.pdf#3]", "pdf", "informe.pdf", {"page": 3, "frag": "3"}),
    ("[code: app.py#L10-L25]", "code", "app.py", {"lines": [10, 25], "frag": "L10-L25"}),
    ("[nota: Mostrar el gráfico de ventas]", "nota", None, {"note": "Mostrar el gráfico de ventas"}),
])
def test_parser_cada_tipo(text: str, kind: str, file: str | None, extra: dict) -> None:
    (r,) = refs(f"Hola {text} mundo")
    assert r["kind"] == kind and r["file"] == file
    for k, v in extra.items():
        assert r[k] == v, k
    assert r["text"] == text
    assert r["at_idx"] == 1   # la palabra que sigue: «mundo»
    assert r["label"].startswith(f"{kind}: ")


def test_parser_valores_por_defecto_y_fragmentos() -> None:
    assert refs("[pdf: a.pdf] x")[0]["page"] == 1
    assert refs("[ppt: a.pptx] x")[0]["page"] == 1
    assert refs("[code: a.py] x")[0]["lines"] is None
    assert refs("[code: a.py#L7] x")[0]["lines"] == [7, 7]
    assert refs("[code: a.py#12-14] x")[0]["lines"] == [12, 14]
    bad = refs("[pdf: a.pdf#tres] x")[0]
    assert bad["page"] is None and bad["bad_frag"] is True
    assert refs("[code: a.py#L9-L3] x")[0]["bad_frag"] is True
    # El prefijo no distingue mayúsculas; una imagen no tiene fragmento.
    assert refs("[IMG: Foto #1.png] x")[0]["file"] == "Foto #1.png"
    assert refs(r"[pdf: C:\docs\informe.pdf#2] x")[0]["label"] == "pdf: informe.pdf"


def test_parser_no_rompe_directivas_ni_tokens() -> None:
    p = parse_script("[Intro]\nHola [img:] a todos [vídeo: x.mp4] y [nota:] fin")
    check_invariants(p)
    kinds = [s["type"] for s in p.segments if s["type"] != "word"]
    # Sin nombre o con un prefijo desconocido, sigue siendo directiva (y sección).
    assert kinds == ["directive", "directive", "directive", "directive"]
    assert [t.text for t in p.tokens] == ["Hola", "a", "todos", "y", "fin"]


def test_referencia_sola_en_su_renglon_no_cambia_los_saltos() -> None:
    def shape(t):
        return [(s["type"], s.get("kind") if s["type"] != "word" else s["text"])
                for s in parse_script(t).segments if s["type"] != "ref"]
    for with_ref, without in [("A\n[img: x.png]\nB", "A\nB"),
                              ("A\n[img: x.png]\n\nB", "A\n\nB"),
                              ("A\n\n[img: x.png]\nB", "A\n\nB"),
                              ("A [img: x.png] B", "A B"),
                              ("A [img: x.png]\nB", "A\nB")]:
        assert shape(with_ref) == shape(without), with_ref


def test_las_referencias_no_son_secciones_ni_tokens_y_la_voz_las_ignora() -> None:
    texto = EJEMPLO.read_text(encoding="utf-8")
    p = parse_script(texto)
    check_invariants(p)
    rs = [s for s in p.segments if s["type"] == "ref"]
    assert sorted(r["kind"] for r in rs) == ["code", "img", "nota", "pdf", "ppt"]
    # Ningún token sale de una referencia: lo que se lee en voz alta es el texto sin ellas.
    sin_refs = "\n".join(l for l in texto.splitlines() if not l.strip().startswith(("[img", "[ppt", "[pdf", "[code", "[nota")))
    assert [t.text for t in p.tokens] == [t.text for t in parse_script(sin_refs).tokens]
    assert not any(":" in t.text and t.text.split(":")[0].lower() in {"img", "ppt", "pdf", "code", "nota"}
                   for t in p.tokens)
    # La voz recorre el guion de punta a punta pasando por encima de cada referencia.
    m = ScriptMatcher(p.tokens)
    words = [t.text for t in p.tokens]
    for i in range(0, len(words), 4):
        m.update(to_units(" ".join(words[max(0, i - 4):i + 4])))
    assert m.current_idx >= len(words) - 4
    seen = {r["kind"]: r["at_idx"] for r in rs}
    assert all(m.current_idx > at for at in seen.values())


# ---------------------------------------------------------------- servidor: /api/ref

@pytest.fixture()
def guiones(tmp_path, monkeypatch) -> Path:
    from PIL import Image

    monkeypatch.setattr(server_mod, "SCRIPTS_DIR", tmp_path)
    (tmp_path / "charla.txt").write_text("Hola [img: foto.png] mundo", encoding="utf-8")
    res = tmp_path / "charla_recursos"
    res.mkdir()
    Image.new("RGB", (64, 48), "#D9B04C").save(res / "foto.png")
    Image.new("RGB", (30, 30), "#000000").save(tmp_path / "suelta.png")
    pages = [Image.new("RGB", (100, 140), c) for c in ("#FF0000", "#00FF00", "#0000FF")]
    pages[0].save(res / "informe.pdf", format="PDF", save_all=True, append_images=pages[1:])
    (res / "app.py").write_text("\n".join(f"x{i} = {i}" for i in range(1, 31)) + "\n", encoding="utf-8")
    (res / "binario.py").write_bytes(b"\x00\x01\x02")
    return tmp_path


@pytest.fixture()
def client(guiones):
    with TestClient(server_mod.create_app(Settings(fake_stt=True))) as c:
        yield c


def get(client, **q):
    return client.get("/api/ref", params={"guion": "charla.txt", **q})


def png_color(data: bytes) -> tuple:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB").getpixel((im.width // 2, im.height // 2))


def near(c: tuple, target: tuple, tol: int = 8) -> bool:
    """Pillow guarda las páginas del PDF en JPEG: el color llega casi exacto."""
    return all(abs(a - b) <= tol for a, b in zip(c, target))


def test_imagen_en_recursos_junto_al_guion_y_absoluta(client, guiones) -> None:
    r = get(client, tipo="img", archivo="foto.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert png_color(r.content) == (0xD9, 0xB0, 0x4C)
    r = get(client, tipo="img", archivo="suelta.png")   # junto al guion
    assert r.status_code == 200 and png_color(r.content) == (0, 0, 0)
    r = get(client, tipo="img", archivo=str(guiones / "charla_recursos" / "foto.png"), guion=None)
    assert r.status_code == 200   # ruta absoluta
    # Sin nombre de guion no hay carpeta _recursos: no se inventa.
    assert client.get("/api/ref", params={"tipo": "img", "archivo": "foto.png"}).status_code == 404


def test_orden_de_busqueda_recursos_antes_que_guiones(client, guiones) -> None:
    from PIL import Image

    # El mismo nombre en guiones/ y en charla_recursos/: manda _recursos (R2).
    Image.new("RGB", (20, 20), "#FFFFFF").save(guiones / "foto.png")
    assert png_color(get(client, tipo="img", archivo="foto.png").content) == (0xD9, 0xB0, 0x4C)


def test_archivo_inexistente_dice_no_encontrado(client) -> None:
    for q, shown in [({"tipo": "img", "archivo": "nada.png"}, "nada.png"),
                     ({"tipo": "pdf", "archivo": "informe.pdf", "pagina": 9}, "informe.pdf#9"),
                     ({"tipo": "pdf", "archivo": "nada.pdf", "pagina": 2}, "nada.pdf#2"),
                     ({"tipo": "code", "archivo": "app.py", "desde": 90, "hasta": 95}, "app.py#L90-L95"),
                     ({"tipo": "code", "archivo": "binario.py"}, "binario.py"),
                     ({"tipo": "img", "archivo": "app.py"}, "app.py"),        # extensión de otro tipo
                     ({"tipo": "exe", "archivo": "foto.png"}, "foto.png")]:
        r = get(client, **q)
        assert r.status_code == 404, q
        assert r.json()["detail"] == f"No encontrado: {shown}", q


def test_pdf_pagina(client) -> None:
    r = get(client, tipo="pdf", archivo="informe.pdf", pagina=2)
    assert r.status_code == 200
    assert near(png_color(r.content), (0, 255, 0))
    assert near(png_color(get(client, tipo="pdf", archivo="informe.pdf", pagina=3).content), (0, 0, 255))


def test_codigo_coloreado_y_escapado(client, guiones) -> None:
    r = get(client, tipo="code", archivo="app.py", desde=10, hasta=12)
    d = r.json()
    assert r.status_code == 200 and d["desde"] == 10 and d["hasta"] == 12 and d["total"] == 30
    assert "x10" in d["html"] and "x12" in d["html"] and "x13" not in d["html"]
    assert 'class="c-' in d["html"]
    (guiones / "charla_recursos" / "t.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    html = get(client, tipo="code", archivo="t.html").json()["html"]
    assert "<script>" not in html and "&lt;" in html


def test_peticion_de_otro_sitio_se_rechaza(client) -> None:
    r = client.get("/api/ref", params={"tipo": "img", "archivo": "foto.png", "guion": "charla.txt"},
                   headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_diapositiva_sin_motor_pide_exportarla(client, guiones, monkeypatch) -> None:
    (guiones / "charla_recursos" / "deck.pptx").write_bytes(b"PK\x03\x04 no importa")
    monkeypatch.setattr(slides_engine, "powerpoint_available", lambda: False)
    monkeypatch.setattr(slides_engine, "find_soffice", lambda: None)
    r = get(client, tipo="ppt", archivo="deck.pptx", pagina=5)
    assert r.status_code == 501
    assert r.json() == {"detail": "Exporta la diapositiva como imagen: deck.pptx#5", "code": "SIN_MOTOR"}


@pytest.mark.skipif(slides_engine.engine() is None, reason="sin PowerPoint ni LibreOffice")
def test_diapositiva_con_el_motor_local(client, guiones) -> None:
    import shutil

    shutil.copy(RECURSOS / "presentacion.pptx", guiones / "charla_recursos" / "presentacion.pptx")
    r = get(client, tipo="ppt", archivo="presentacion.pptx", pagina=2)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert get(client, tipo="ppt", archivo="presentacion.pptx", pagina=3).json()["detail"] == \
        "No encontrado: presentacion.pptx#3"


def test_cache_se_rehace_si_cambia_el_archivo(client, guiones) -> None:
    from PIL import Image

    foto = guiones / "charla_recursos" / "foto.png"
    r1 = get(client, tipo="img", archivo="foto.png")
    assert r1.headers["x-tp-rehecha"] == "1"
    r2 = get(client, tipo="img", archivo="foto.png")
    assert r2.headers["x-tp-rehecha"] == "0" and r2.content == r1.content
    cache = guiones / ".cache"
    assert len(list(cache.glob("*.png"))) == 1
    # Cambia el archivo (mismo tamaño en píxeles, otro color): se rehace y se ve el nuevo.
    Image.new("RGB", (64, 48), "#123456").save(foto)
    st = foto.stat()
    os.utime(foto, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    r3 = get(client, tipo="img", archivo="foto.png")
    assert r3.headers["x-tp-rehecha"] == "1"
    assert png_color(r3.content) == (0x12, 0x34, 0x56)
    assert len(list(cache.glob("*.png"))) == 2   # la vieja no se borra


def test_cache_se_rehace_si_sube_la_version(client, monkeypatch) -> None:
    assert get(client, tipo="img", archivo="foto.png").headers["x-tp-rehecha"] == "1"
    assert get(client, tipo="img", archivo="foto.png").headers["x-tp-rehecha"] == "0"
    monkeypatch.setattr(ref_thumbs, "THUMB_GEN", ref_thumbs.THUMB_GEN + 1)
    assert get(client, tipo="img", archivo="foto.png").headers["x-tp-rehecha"] == "1"


# ---------------------------------------------------------------- /control (R5)

@pytest.mark.parametrize("value", ["texto", "referencias", "siguiente"])
def test_orden_view(value: str) -> None:
    c = parse_control_message(json.dumps({"v": 1, "cmd": "view", "value": value}))
    assert (c.cmd, c.value) == ("view", value)


@pytest.mark.parametrize("value", [None, "", "Texto", "imagenes", 1])
def test_orden_view_invalida(value) -> None:
    with pytest.raises(ProtocolError):
        parse_control_message(json.dumps({"v": 1, "cmd": "view", "value": value}))


def test_orden_ref_zoom() -> None:
    c = parse_control_message('{"v":1,"cmd":"ref_zoom"}')
    assert (c.cmd, c.value) == ("ref_zoom", None)


def test_estado_trae_los_campos_nuevos_sin_cambiar_los_viejos() -> None:
    assert ESTADO_FIELDS[-3:] == ("vista", "ref_actual", "ref_ampliada")
    assert ESTADO_FIELDS[:19][0] == "corriendo" and ESTADO_FIELDS[18] == "cuenta"
    e = sanitize_estado({"vista": "referencias", "ref_actual": "ppt: informe.pptx", "ref_ampliada": True})
    assert (e["vista"], e["ref_actual"], e["ref_ampliada"]) == ("referencias", "ppt: informe.pptx", True)
    e = sanitize_estado({"vista": "imagenes", "ref_actual": 5, "ref_ampliada": "sí"})
    assert (e["vista"], e["ref_actual"], e["ref_ampliada"]) == (None, None, None)   # sin dato, null
    msg = estado_control_msg(None, conectado=False)
    assert msg["vista"] is None and msg["ref_actual"] is None and msg["ref_ampliada"] is None


def test_guion_de_ejemplo_trae_sus_recursos() -> None:
    texto = EJEMPLO.read_text(encoding="utf-8")
    for r in [s for s in parse_script(texto).segments if s["type"] == "ref" and s["file"]]:
        assert (RECURSOS / r["file"]).is_file(), r["file"]
    assert time.time()  # (el ejemplo se dibuja de punta a punta en test_e2e_referencias.py)
