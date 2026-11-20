"""Hito 84 (T6) de punta a punta: cada orden del contrato, enviada por /control, la
EJECUTA el visor real (Chrome headless con frontend/app.js) y cambia el estado que
reciben los controles. Mide el artefacto: el estado lo calcula la página, no el test.

Se salta (y lo dice) si no hay Chrome ni Edge.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets

import e2e_cdp

GUION = """Buenas tardes a todos. Hoy vamos a presentar las fases uno, dos y tres del proyecto.

[LÁMINA 2 · Arquitectura]
En la fase uno revisaremos la arquitectura general del sistema y los componentes principales del producto.

[LÁMINA 3 · Voz local]
En la fase dos demostraremos el funcionamiento del módulo de reconocimiento de voz local, totalmente integrado.

[LÁMINA 4 · Manos libres]
En la fase tres mostraremos el comportamiento del teleprompter en modo manos libres, avanzando al ritmo de la lectura.

Muchas gracias por su atención."""

pytestmark = pytest.mark.skipif(e2e_cdp.find_chrome() is None, reason="sin Chrome ni Edge")


class Ctl:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.last: dict | None = None

    async def send(self, cmd: str, value=None) -> None:
        msg = {"v": 1, "cmd": cmd}
        if value is not None:
            msg["value"] = value
        await self.ws.send(json.dumps(msg))

    async def until(self, pred, timeout: float = 4.0) -> dict:
        """Primer estado que cumple `pred` (los anteriores se descartan)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            left = end - loop.time()
            if left <= 0:
                raise AssertionError(f"no llegó el estado esperado; último: {self.last}")
            try:
                m = json.loads(await asyncio.wait_for(self.ws.recv(), left))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == "estado":
                self.last = m
                if pred(m):
                    return m


async def _scenario(port: int) -> None:
    async with e2e_cdp.chrome(port) as cdp:
        await e2e_cdp.open_app(cdp, port)
        await e2e_cdp.load_script(cdp, GUION)
        async with websockets.connect(f"ws://127.0.0.1:{port}/control") as a, \
                websockets.connect(f"ws://127.0.0.1:{port}/control") as b:
            c, c2 = Ctl(a), Ctl(b)
            e = await c.until(lambda m: m["conectado"] and m["secciones"] == 3)
            # Manual es el modo predeterminado.
            assert e["modo"] == "manual" and e["corriendo"] is False

            async def check(cmd, value, pred, what):
                await c.send(cmd, value)
                try:
                    return await c.until(pred)
                except AssertionError as exc:
                    raise AssertionError(f"{cmd} {value}: {what} ({exc}); errores JS: {cdp.errors}") from None

            v = e["velocidad"]
            await check("set_speed", 10, lambda m: m["velocidad"] == 10 and m["ppm"] == 300, "velocidad 10")
            await check("faster", None, lambda m: m["velocidad"] == 11, "más rápido")
            await check("slower", None, lambda m: m["velocidad"] == 10, "más lento")
            letra = e["letra"]
            await check("font", 1, lambda m: m["letra"] == letra + 1, "letra +1")
            await check("font", -1, lambda m: m["letra"] == letra, "letra -1")
            await check("mirror", None, lambda m: m["espejo"] is True, "espejo")
            await check("mirror", None, lambda m: m["espejo"] is False, "espejo de vuelta")
            assert e["banda"] is True   # encendida por defecto (diseño v3)
            await check("band", None, lambda m: m["banda"] is False, "banda")
            await check("halo", None, lambda m: m["halo"] is True, "halo")
            await check("backdrop", -5, lambda m: m["fondo_pct"] == 95, "fondo -5")
            await check("backdrop", 5, lambda m: m["fondo_pct"] == 100, "fondo +5")
            await check("mode", "voz", lambda m: m["modo"] == "voz", "modo voz")
            await check("mode", "siguiente", lambda m: m["modo"] == "manual", "siguiente (voz -> manual)")
            await check("mode", "asistido", lambda m: m["modo"] == "asistido", "modo asistido")
            await check("mode", "manual", lambda m: m["modo"] == "manual", "modo manual")

            # Posición
            await check("next_section", None, lambda m: m["seccion_idx"] == 0 and m["progreso"] > 0, "sección 1")
            s1 = c.last["progreso"]
            await check("next_section", None, lambda m: m["seccion_idx"] == 1 and m["progreso"] > s1, "sección 2")
            p = c.last["progreso"]
            await check("scroll", 2, lambda m: m["progreso"] > p, "dial +2")
            p = c.last["progreso"]
            await check("scroll", -1, lambda m: m["progreso"] < p, "dial -1")
            p = c.last["progreso"]
            await check("back_lines", 3, lambda m: m["progreso"] < p, "atrás 3 líneas")
            # Tras retroceder 3 líneas se vuelve a la sección 1: la siguiente es la 2, luego la 3.
            await check("next_section", None, lambda m: m["seccion_idx"] == 1, "de vuelta a sección 2")
            await check("next_section", None, lambda m: m["seccion_idx"] == 2, "sección 3")
            await c.send("scroll", 1)
            await c.until(lambda m: True)
            p = c.last["progreso"]
            await check("prev_paragraph", None, lambda m: m["progreso"] < p, "párrafo anterior")
            await check("top", None, lambda m: m["progreso"] == 0, "inicio")

            # Marcha y cuenta atrás
            await check("play_pause", None, lambda m: m["corriendo"] is True, "play")
            await asyncio.sleep(1.2)
            await check("play_pause", None, lambda m: m["corriendo"] is False and m["progreso"] > 0, "pausa")
            await check("countdown", 3, lambda m: m["cuenta"] in (3, 2), "cuenta atrás visible")
            await c.until(lambda m: m["cuenta"] is None and m["corriendo"] is True, timeout=6)
            await check("play_pause", None, lambda m: m["corriendo"] is False, "pausa tras la cuenta")

            # Ventana: en el navegador no hay app nativa: sin dato, null (nunca un falso false).
            await check("lock", None, lambda m: m["bloqueado"] is None, "bloquear en navegador")
            await check("topmost", None, lambda m: m["adelante"] is None, "adelante en navegador")

            # open_script lo resuelve el servidor y lo dibuja el visor.
            await check("open_script", "ejemplo", lambda m: m["guion"] == "ejemplo.txt"
                        and m["secciones"] == 0 and m["progreso"] == 0, "abrir guion")

            # Varios controles a la vez: el segundo también recibe el estado.
            e2 = await c2.until(lambda m: m["guion"] == "ejemplo.txt")
            assert e2["conectado"] is True
            # Atajos de teclado siguen: M cambia de modo.
            await cdp.ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'm', bubbles: true})); true")
            await c.until(lambda m: m["modo"] == "asistido")
            # El visor sigue siendo uno solo y sigue conectado.
            assert await cdp.ev("document.getElementById('chip-conn').textContent") == "Servidor"
            assert v  # la velocidad inicial existía (sin dato habría sido None)
        assert not cdp.errors, cdp.errors
        assert not cdp.external(), cdp.external()   # ninguna petición fuera de 127.0.0.1


def test_cada_orden_del_contrato_cambia_el_estado() -> None:
    port = e2e_cdp.free_port()
    with e2e_cdp.server(port):
        asyncio.run(_scenario(port))
