# PROGRESS LOG

## 2026-05-15 — Inicialización B.L.A.S.T.
- Estructura de carpetas creada: `architecture/`, `tools/`, `backend/`, `frontend/`, `scripts/`, `logs/`, `guiones/`.
- Constitución `gemini.md` redactada con Data Schema congelado.
- Stack confirmado: Python 3.11 + FastAPI + faster-whisper + JS vanilla.

## Fase 1 — Blueprint
- Discovery cerrado: entrada = micrófono default, guion = textarea/paste, salida = WebSocket JSON.
- Schema de payload `scroll` definido (idx, matched_word, confidence, latency_ms).

## Fase 2 — Link
- `tools/check_cuda.py`: chequea torch.cuda + nombre de GPU.
- `tools/check_audio.py`: lista dispositivos de entrada.
- `tools/check_whisper.py`: carga `medium` en CUDA y transcribe 1 seg de silencio.

## Fase 3 — Architect
- SOPs escritos en `architecture/`.
- `audio_capture.py`: thread productor → cola de chunks 500 ms.
- `stt_engine.py`: consume cola, devuelve texto + timestamps.
- `script_matcher.py`: sliding window con normalización Unicode.
- `backend/server.py`: orquesta los tres módulos, expone `/ws`.

## Fase 4 — Stylize
- UI con tipografía 64px default, contraste alto (texto blanco sobre fondo negro).
- Textos por delante con opacidad reducida; palabra actual resaltada; texto leído atenuado.
- Smooth scroll vía `element.scrollIntoView({behavior: 'smooth', block: 'center'})`.

## Fase 5 — Trigger
- `requirements.txt` con versiones congeladas.
- `scripts/install.bat`: crea venv, instala torch CUDA + faster-whisper + FastAPI.
- `scripts/start.bat`: activa venv, lanza uvicorn, abre navegador en `http://127.0.0.1:8765`.

## Pruebas de Latencia (a ejecutar tras instalación)
- [ ] STT cold start (carga modelo): _pendiente medir_
- [ ] STT warm chunk 500 ms → texto: _pendiente medir, objetivo < 200 ms_
- [ ] Matching texto → idx: _pendiente medir, objetivo < 5 ms_
- [ ] WebSocket round-trip: _pendiente medir, objetivo < 10 ms en localhost_
- [ ] Total end-to-end: _pendiente medir, objetivo < 300 ms_
