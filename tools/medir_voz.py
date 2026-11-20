r"""Hito 84 (T4): visor de medición de la voz (de V0, con el modo y los frenos).

Servidor con TP_AUDIO_FILE; este cliente carga el guion, arranca la voz y registra
cada `scroll` con su tiempo. Lo analiza analizar_voz.py de V0 (DIAGNOSTICO_84 §6).

    python tools\medir_voz.py PUERTO GUION SEGUNDOS SALIDA.json [v2|voz|asistido]

`v2` manda `start` sin modo (el comportamiento de antes, sin frenos); `voz` manda
el modo Voz con los frenos por defecto del visor.
"""
import asyncio
import json
import sys
import time

import websockets

PORT, GUION, DUR, OUT = int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]
MODO = sys.argv[5] if len(sys.argv) > 5 else "voz"
FRENOS = {"confirm_words": 3, "max_ahead": 6, "max_wps": 6, "sentence_guard": True}


async def main() -> None:
    texto = open(GUION, encoding="utf-8").read()
    log = []
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", max_size=None) as ws:
        await ws.send(json.dumps({"action": "hello", "client": "medicion-84"}))
        await ws.send(json.dumps({"action": "load_script", "text": texto, "seq": 1}))
        t_load = time.time()
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 600))
            if m["type"] == "state" and m["model"] == "ready":
                break
            if m["type"] == "status" and m["state"] == "ready":
                break
            if m["type"] == "status" and m["state"] == "error":
                sys.exit(f"error: {m}")
        print("modelo listo en", round(time.time() - t_load, 1), "s", flush=True)
        start = {"action": "start", "idx": 0, "seq": 2}
        if MODO != "v2":
            start["mode"] = MODO
            if MODO == "voz":
                start["brakes"] = FRENOS
        await ws.send(json.dumps(start))
        t0 = time.monotonic()
        while time.monotonic() - t0 < DUR:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
            except asyncio.TimeoutError:
                continue
            if m["type"] in ("scroll", "transcript", "status", "activity"):
                m["t_ms"] = int((time.monotonic() - t0) * 1000)
                log.append(m)
        await ws.send(json.dumps({"action": "pause"}))
    json.dump(log, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(len(log), "mensajes;", sum(1 for m in log if m["type"] == "scroll"), "scroll", flush=True)


asyncio.run(main())
