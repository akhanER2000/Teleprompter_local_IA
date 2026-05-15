"""Servidor FastAPI: orquesta audio + STT + matcher y expone WebSocket."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Permitir imports desde la raíz del proyecto
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.audio_capture import AudioCapture  # noqa: E402
from tools.stt_engine import STTEngine  # noqa: E402
from tools.script_matcher import ScriptMatcher  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("teleprompter")

MODEL_SIZE = os.environ.get("TP_MODEL_SIZE", "medium")
DEVICE = os.environ.get("TP_DEVICE", "cuda")
COMPUTE = os.environ.get("TP_COMPUTE", "float16")
LANGUAGE = os.environ.get("TP_LANGUAGE", "es")


app = FastAPI(title="Teleprompter Local IA")

FRONTEND_DIR = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


class Pipeline:
    def __init__(self):
        self.stt = STTEngine(model_size=MODEL_SIZE, device=DEVICE,
                             compute_type=COMPUTE, language=LANGUAGE)
        self.matcher = ScriptMatcher()
        self.audio_queue: asyncio.Queue | None = None
        self.capture: AudioCapture | None = None
        self.worker_task: asyncio.Task | None = None
        self.running = False
        self.client: WebSocket | None = None

    async def boot(self):
        log.info("Cargando modelo STT '%s' en %s (%s)...", MODEL_SIZE, DEVICE, COMPUTE)
        t0 = time.time()
        await asyncio.to_thread(self.stt.load)
        log.info("Modelo cargado en %.1f s.", time.time() - t0)

    async def attach_client(self, ws: WebSocket):
        if self.client is not None:
            await ws.close(code=1008, reason="Solo un cliente permitido")
            return False
        self.client = ws
        await self._send({"type": "status", "state": "ready",
                          "message": f"Modelo {MODEL_SIZE} listo en {DEVICE}"})
        return True

    async def detach_client(self):
        self.client = None
        await self.stop_listening()

    async def _send(self, payload: dict):
        if self.client is None:
            return
        try:
            await self.client.send_text(json.dumps(payload))
        except Exception:
            pass

    async def start_listening(self):
        if self.running:
            return
        if not self.matcher.tokens:
            await self._send({"type": "status", "state": "error",
                              "message": "Carga un guion antes de iniciar."})
            return
        self.running = True
        self.audio_queue = asyncio.Queue(maxsize=4)
        loop = asyncio.get_running_loop()
        self.capture = AudioCapture(self.audio_queue, loop)
        try:
            self.capture.start()
        except Exception as e:
            self.running = False
            await self._send({"type": "status", "state": "error",
                              "message": f"Micrófono: {e}"})
            return
        self.worker_task = asyncio.create_task(self._worker())
        await self._send({"type": "status", "state": "listening",
                          "message": "Escuchando..."})

    async def stop_listening(self):
        self.running = False
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if self.worker_task is not None:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self.worker_task = None
        await self._send({"type": "status", "state": "paused", "message": "Pausado."})

    async def _worker(self):
        assert self.audio_queue is not None
        while self.running:
            try:
                chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                tr = await self.stt.transcribe(chunk.samples, chunk.timestamp_ms)
            except Exception as e:
                log.exception("STT error: %s", e)
                continue
            if not tr.text:
                continue
            await self._send({"type": "transcript", "text": tr.text, "is_final": False})
            result = self.matcher.update(tr.text)
            await self._send({
                "type": "scroll",
                "current_idx": result.new_idx,
                "matched_word": result.matched_word,
                "confidence": result.confidence,
                "latency_ms": tr.latency_ms,
            })

    async def handle_action(self, msg: dict):
        action = msg.get("action")
        if action == "load_script":
            text = msg.get("text", "")
            self.matcher.load_script(text)
            await self._send({"type": "status", "state": "ready",
                              "message": f"Guion cargado: {len(self.matcher.tokens)} palabras."})
            await self._send({"type": "scroll", "current_idx": 0,
                              "matched_word": None, "confidence": 1.0, "latency_ms": 0})
        elif action == "start":
            await self.start_listening()
        elif action == "pause":
            await self.stop_listening()
        elif action == "reset":
            self.matcher.reset(0)
            self._drain_audio_queue()
            await self._send({"type": "scroll", "current_idx": 0,
                              "matched_word": None, "confidence": 1.0, "latency_ms": 0})
        elif action == "set_position":
            idx = int(msg.get("idx", 0))
            self.matcher.reset(idx)
            self._drain_audio_queue()
            await self._send({"type": "scroll", "current_idx": self.matcher.current_idx,
                              "matched_word": None, "confidence": 1.0, "latency_ms": 0})
        elif action == "step":
            delta = int(msg.get("delta", 0))
            self.matcher.reset(self.matcher.current_idx + delta)
            self._drain_audio_queue()
            await self._send({"type": "scroll", "current_idx": self.matcher.current_idx,
                              "matched_word": None, "confidence": 1.0, "latency_ms": 0})

    def _drain_audio_queue(self):
        """Descarta chunks de audio ya capturados pero aún sin transcribir.
        Tras un reposicionamiento manual, el audio buffereado pertenece al
        instante previo al salto y reintroduciría la posición vieja."""
        if self.audio_queue is None:
            return
        try:
            while True:
                self.audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass


pipeline = Pipeline()


@app.on_event("startup")
async def _on_startup():
    await pipeline.boot()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ok = await pipeline.attach_client(ws)
    if not ok:
        return
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await pipeline.handle_action(msg)
    except WebSocketDisconnect:
        pass
    finally:
        await pipeline.detach_client()
