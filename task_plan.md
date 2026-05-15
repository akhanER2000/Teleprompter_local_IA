# TASK PLAN — Teleprompter Local IA

## Fase 0: Inicialización de Memoria
- [x] gemini.md (Constitución + Data Schema)
- [x] task_plan.md
- [x] findings.md
- [x] progress.md

## Fase 1: B - Blueprint
- [x] Discovery: micrófono default sistema, guion vía paste/upload, salida JSON via WebSocket
- [x] Data-First: payload congelado en gemini.md §4
- [x] Research: forced alignment vía sliding window de transcripción + matching difuso por normalización

## Fase 2: L - Link
- [x] tools/check_cuda.py — verificar CUDA + GPU
- [x] tools/check_audio.py — verificar micrófono
- [x] tools/check_whisper.py — cargar modelo y transcribir 1 seg de ruido

## Fase 3: A - Architect
- [x] architecture/SOP_01_audio_pipeline.md
- [x] architecture/SOP_02_matching_algorithm.md
- [x] architecture/SOP_03_websocket_protocol.md
- [x] tools/audio_capture.py
- [x] tools/stt_engine.py
- [x] tools/script_matcher.py
- [x] backend/server.py (FastAPI + WebSocket)

## Fase 4: S - Stylize
- [x] frontend/index.html
- [x] frontend/styles.css
- [x] frontend/app.js
- [x] Integración WebSocket → scroll DOM

## Fase 5: T - Trigger
- [x] requirements.txt
- [x] scripts/install.bat (crea venv, instala deps)
- [x] scripts/start.bat (lanza backend + abre navegador)
- [x] README.md (instrucciones rápidas)
