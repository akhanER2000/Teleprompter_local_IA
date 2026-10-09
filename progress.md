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

## 2026-10-09 · Pulido integral, Fase 0: preparación y línea base

Rama `fix/pulido-integral`, creada desde `main` tras adelantarla (fast-forward) a `origin/main`, que tenía un commit más (licencia MIT).

**Línea base encontrada**
- El `.venv` anterior está roto: su intérprete base (Python 3.9.13 de `Visual Studio\Shared\Python39_64`) ya no existe, así que no arrancan ni `pip freeze`, ni `scripts\handshake.bat`, ni la app. El inventario de paquetes se reconstruyó desde los `*.dist-info` en `docs/baseline_freeze.txt` (66 paquetes; `.venv` pesa 4,7 GB, de ellos 4,3 GB de `torch 2.5.1+cu121`).
- Pythons disponibles: 3.12.10 y 3.14. No hay 3.11.

**Decisiones confirmadas por el usuario (sección 6)**
- D1: entorno nuevo `.venv312` con Python 3.12, sin torch; `.venv` queda intacto como respaldo.
- D2: faster-whisper 1.2.1; `medium` sigue por defecto hasta que el replay demuestre una mejora de `large-v3-turbo` (ya está en la caché local).
- D3 y D4: prueba de viabilidad del bloqueo de clics y de la opacidad; se implementa lo que funcione y se quita de la documentación lo que no.
- D5: `gemini.md` pasa a ser un puntero breve a `CLAUDE.md`.

**Entorno nuevo (`.venv312`, 1,1 GB)**
- Dependencias directas fijadas en `requirements.txt` y de desarrollo en `requirements-dev.txt`; árbol completo en `requirements.lock.txt`. Se quitó `python-multipart` (no se usa).
- CTranslate2 sin torch no encuentra `cublas64_12.dll`: se agregó `nvidia-cublas-cu12==12.8.4.1` y `tools/cuda_dlls.py`, que registra `site-packages/nvidia/*/bin` en el buscador de DLL y en el PATH. La `cudnn64_9.dll` que trae CTranslate2 alcanza para Whisper (verificado).

**Mediciones (RTX 5070 Ti, `medium`, float16, `beam_size=1`)**
- Carga del modelo: 1,7 s.
- Primera inferencia de un proceso: 21 s la primera vez en la máquina (CTranslate2 4.7.1 compila PTX para sm_120 y lo guarda en `%APPDATA%\NVIDIA\ComputeCache`); 0,66 s en arranques posteriores, con la caché.
- Inferencias siguientes: ~100 ms para 1 s de audio y ~85 ms para una ventana de 3 s. Hay margen de sobra para ventanas solapadas cada 300 a 400 ms.

**Herramientas y control**
- `tools/record_sample.py` (graba WAV de 16 kHz mono en `tests/fixtures/audio/`, ignorada por git), `tests/conftest.py` y `tests/test_entorno.py` (verifica que lo instalado coincide con lo fijado y que no hay torch): 12 pruebas pasan.
- Centro de control: `CLAUDE.md`, `CLAUDE.local.md` (ignorado), `.claude/settings.json`, `.claude/commands/` (`/test`, `/run`, `/replay`, `/qa`), `.claude/rules/`, agente `revisor-reglas` y skills seleccionadas del catálogo en `.claude/skills/`.
- `docs/diseno_v2.md`: contrato técnico que siguen todas las fases.

**Desviaciones del plan, justificadas**
- El venv nuevo se crea en la Fase 0 y no en la 5, porque el anterior no arranca y sin él no hay pruebas ni smoke tests. La Fase 5 conserva el resto (instalador, `check_cuda` sin torch, lanzadores, respaldo del venv viejo).
- La conversión de números a palabras (`num2words`) se adelanta a la Fase 1, porque el parser nuevo ya la necesita para que sus índices y unidades sean definitivos.

## Pruebas de Latencia (a ejecutar tras instalación)
- [ ] STT cold start (carga modelo): _pendiente medir_
- [ ] STT warm chunk 500 ms → texto: _pendiente medir, objetivo < 200 ms_
- [ ] Matching texto → idx: _pendiente medir, objetivo < 5 ms_
- [ ] WebSocket round-trip: _pendiente medir, objetivo < 10 ms en localhost_
- [ ] Total end-to-end: _pendiente medir, objetivo < 300 ms_
