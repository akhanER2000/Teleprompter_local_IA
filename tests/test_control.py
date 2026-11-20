"""Hito 84 (T6): endpoint /control con el contrato exacto, sin romper /ws.

Lado servidor: validación de órdenes, reenvío al visor, difusión del estado a
varios controles (al cambiar y cada segundo), `open_script` y origen. Que el
visor EJECUTE cada orden se prueba en el navegador real (tests/test_e2e_control.py).
"""
from __future__ import annotations

import contextlib
import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from backend import server as server_mod
from backend.protocol import ESTADO_FIELDS, ProtocolError, parse_control_message
from test_protocol import SCRIPT, Ws, make  # noqa: F401  (fixture)

CONTRACT = [
    ({"cmd": "play_pause"}, None),
    ({"cmd": "faster"}, None),
    ({"cmd": "slower"}, None),
    ({"cmd": "set_speed", "value": 1}, 1),
    ({"cmd": "set_speed", "value": 20}, 20),
    ({"cmd": "prev_paragraph"}, None),
    ({"cmd": "back_lines", "value": 3}, 3),
    ({"cmd": "next_section"}, None),
    ({"cmd": "top"}, None),
    ({"cmd": "scroll", "value": -5}, -5),
    ({"cmd": "scroll", "value": 5}, 5),
    ({"cmd": "font", "value": 1}, 1),
    ({"cmd": "font", "value": -1}, -1),
    ({"cmd": "mirror"}, None),
    ({"cmd": "mode", "value": "manual"}, "manual"),
    ({"cmd": "mode", "value": "asistido"}, "asistido"),
    ({"cmd": "mode", "value": "voz"}, "voz"),
    ({"cmd": "mode", "value": "siguiente"}, "siguiente"),
    ({"cmd": "backdrop", "value": 5}, 5),
    ({"cmd": "backdrop", "value": -5}, -5),
    ({"cmd": "band"}, None),
    ({"cmd": "halo"}, None),
    ({"cmd": "lock"}, None),
    ({"cmd": "topmost"}, None),
    ({"cmd": "countdown", "value": 3}, 3),
]

BAD = [
    "no es json", "[]", '{"cmd":"play_pause"}', '{"v":2,"cmd":"play_pause"}',
    '{"v":true,"cmd":"play_pause"}', '{"v":1,"cmd":"bailar"}', '{"v":1}',
    '{"v":1,"cmd":"set_speed","value":0}', '{"v":1,"cmd":"set_speed","value":21}',
    '{"v":1,"cmd":"set_speed","value":2.5}', '{"v":1,"cmd":"set_speed","value":true}',
    '{"v":1,"cmd":"scroll","value":6}', '{"v":1,"cmd":"scroll","value":0}',
    '{"v":1,"cmd":"font","value":2}', '{"v":1,"cmd":"font","value":true}',
    '{"v":1,"cmd":"mode","value":"turbo"}', '{"v":1,"cmd":"backdrop","value":0}',
    '{"v":1,"cmd":"countdown","value":-1}', '{"v":1,"cmd":"open_script","value":"../x.txt"}',
    '{"v":1,"cmd":"open_script","value":"C:\\\\x.txt"}', '{"v":1,"cmd":"open_script","value":".."}',
    '{"v":1,"cmd":"open_script","value":""}',
]


class Control(Ws):
    def order(self, **msg) -> None:
        self.ws.send_text(json.dumps({"v": 1, **msg}))

    def estado(self, timeout: float = 5.0, **fields) -> dict:
        msg, _ = self.wait_for(lambda m: m.get("type") == "estado"
                               and all(m.get(k) == v for k, v in fields.items()), timeout)
        return msg


@contextlib.contextmanager
def control(h, **kw):
    with h.client.websocket_connect("/control", **kw) as ws:
        yield Control(ws)


# ---------------------------------------------------------------- protocolo puro

@pytest.mark.parametrize("msg,value", CONTRACT)
def test_contrato_valido(msg: dict, value) -> None:
    cmd = parse_control_message(json.dumps({"v": 1, **msg}))
    assert (cmd.cmd, cmd.value) == (msg["cmd"], value)


def test_back_lines_y_countdown_sin_valor_usan_3() -> None:
    assert parse_control_message('{"v":1,"cmd":"back_lines"}').value == 3
    assert parse_control_message('{"v":1,"cmd":"countdown"}').value == 3


@pytest.mark.parametrize("raw", BAD)
def test_contrato_invalido(raw: str) -> None:
    with pytest.raises(ProtocolError):
        parse_control_message(raw)


# ---------------------------------------------------------------- servidor

def test_sin_visor_estado_desconectado_con_todos_los_campos(make) -> None:
    h = make()
    with control(h) as c:
        est = c.estado()
        assert est["v"] == 1 and est["conectado"] is False and est["corriendo"] is False
        assert set(ESTADO_FIELDS) <= set(est)
        # Sin dato no se inventa nada: null, nunca 0.
        assert est["velocidad"] is None and est["progreso"] is None
        c.order(cmd="play_pause")   # se descarta y responde con el estado
        assert c.estado()["conectado"] is False


@pytest.mark.parametrize("msg,value", CONTRACT)
def test_cada_orden_llega_al_visor(make, msg: dict, value) -> None:
    h = make()
    with h.connect() as viewer:
        viewer.hello()
        with control(h) as c:
            c.estado()
            c.order(**msg)
            fwd = viewer.wait_type("control")
            assert fwd == {"type": "control", "cmd": msg["cmd"], "value": value}


def test_estado_del_visor_llega_a_varios_controles(make) -> None:
    h = make()
    with h.connect() as viewer, control(h) as a, control(h) as b:
        viewer.hello()
        viewer.send(action="estado", estado={
            "corriendo": True, "modo": "manual", "velocidad": 7, "ppm": 210, "progreso": 0.25,
            "transcurrido_s": 12, "restante_s": 36, "seccion": "[LÁMINA 2]", "seccion_idx": 1,
            "secciones": 4, "letra": 9, "espejo": False, "fondo_pct": 40, "banda": True,
            "halo": True, "bloqueado": True, "adelante": False, "guion": "ejemplo.txt",
            "cuenta": None, "basura": "no pasa"})
        for c in (a, b):
            est = c.estado(conectado=True, velocidad=7)
            assert est["modo"] == "manual" and est["seccion"] == "[LÁMINA 2]"
            assert est["fondo_pct"] == 40 and "basura" not in est


def test_estado_invalido_queda_sin_dato(make) -> None:
    h = make()
    with h.connect() as viewer, control(h) as c:
        viewer.hello()
        viewer.send(action="estado", estado={"velocidad": 99, "modo": "turbo", "progreso": 2,
                                             "espejo": "si", "corriendo": True})
        est = c.estado(conectado=True, corriendo=True)
        assert est["velocidad"] is None and est["modo"] is None
        assert est["progreso"] is None and est["espejo"] is None


def test_estado_se_difunde_cada_segundo(make) -> None:
    h = make()
    with h.connect() as viewer, control(h) as c:
        viewer.hello()
        c.estado()
        t0 = time.monotonic()
        c.estado(timeout=2.5)
        c.estado(timeout=2.5)
        assert time.monotonic() - t0 < 3.0


def test_orden_invalida_responde_error_y_sigue(make) -> None:
    h = make()
    with h.connect() as viewer, control(h) as c:
        viewer.hello()
        c.estado()
        c.ws.send_text('{"v":1,"cmd":"bailar"}')
        err, _ = c.wait_for(lambda m: m.get("type") == "status")
        assert err["state"] == "error" and err["code"] == "BAD_REQUEST" and err["v"] == 1
        c.order(cmd="mirror")
        assert viewer.wait_type("control")["cmd"] == "mirror"


def test_ws_sigue_con_un_solo_visor(make) -> None:
    """Los controles no reemplazan al visor; un segundo /ws sí (regla de siempre)."""
    h = make()
    with h.connect() as v1:
        v1.hello()
        with control(h) as a, control(h) as b:
            a.estado(conectado=True)
            b.estado(conectado=True)
            assert h.session.conn is not None and len(h.session.controls) == 2
            a.order(cmd="halo")
            assert v1.wait_type("control")["cmd"] == "halo"   # v1 sigue siendo el visor
            with h.connect() as v2:
                v2.hello()
                code, _ = v1.wait_close()
                assert code == server_mod.CLOSE_REPLACED
                b.order(cmd="band")
                assert v2.wait_type("control")["cmd"] == "band"
        h.wait(lambda: not h.session.controls, "los controles se dan de baja")


def test_visor_que_se_va_avisa_a_los_controles(make) -> None:
    h = make()
    with control(h) as c:
        with h.connect() as viewer:
            viewer.hello()
            viewer.send(action="estado", estado={"corriendo": True})
            c.estado(conectado=True, corriendo=True)
        est = c.estado(conectado=False)
        assert est["corriendo"] is False


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:9999"])
def test_origen_ajeno_rechazado(make, origin: str) -> None:
    h = make()
    with pytest.raises(WebSocketDisconnect) as exc:
        with h.client.websocket_connect("/control", headers={"origin": origin}) as ws:
            ws.receive_text()
    assert exc.value.code == server_mod.CLOSE_POLICY


def test_origen_propio_aceptado(make) -> None:
    h = make()
    with control(h, headers={"origin": "http://127.0.0.1:8765"}) as c:
        assert c.estado()["type"] == "estado"


def test_open_script(make, tmp_path, monkeypatch) -> None:
    (tmp_path / "charla.txt").write_text(SCRIPT, encoding="utf-8")
    (tmp_path.parent / "fuera.txt").write_text("secreto", encoding="utf-8")
    monkeypatch.setattr(server_mod, "SCRIPTS_DIR", tmp_path)
    h = make()
    with h.connect() as viewer, control(h) as c:
        viewer.hello()
        c.estado()
        c.order(cmd="open_script", value="charla")          # sin .txt también vale
        loaded = viewer.wait_type("script_loaded")
        assert loaded["name"] == "charla.txt" and loaded["total_tokens"] == 200
        c.order(cmd="open_script", value="no_existe.txt")
        err, _ = c.wait_for(lambda m: m.get("type") == "status")
        assert "No existe" in err["message"]
        assert server_mod.resolve_script("../fuera.txt", tmp_path) is None
        assert server_mod.resolve_script("charla.txt", tmp_path) is not None
