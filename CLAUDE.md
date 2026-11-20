# Teleprompter Local IA ("Flow Local")

Teleprompter para presentaciones, grabaciones y videollamadas: el texto avanza al ritmo de la voz (Whisper local en GPU) o en Auto-scroll. Corre 100% en la máquina del usuario (Windows 10/11, RTX 5070 Ti).

## Constitución (reglas innegociables)

1. **100% local.** Ninguna petición de red en tiempo de ejecución (ni fuentes, ni CDN, ni telemetría). La red solo se usa al instalar dependencias y descargar el modelo (`scripts\install.bat`). Bind siempre en `127.0.0.1`.
2. **Frontend vanilla.** HTML, CSS y JS sin frameworks ni build step.
3. **Atomicidad.** Cada módulo de `tools/` hace una sola cosa; la composición vive en `backend/`.
4. **Una sola fuente de verdad para el guion.** El backend parsea; el frontend solo dibuja los `segments`.
5. **Ningún error silencioso.** Todo fallo llega a la UI como `status` con `code` y queda en `logs/teleprompter.log`.
6. **Dependencias justificadas y con versión fijada** (`requirements.txt`, `requirements-dev.txt`).
7. **No borrar** `.venv*`, guiones ni archivos del usuario sin preguntar. Los cambios de entorno se hacen en paralelo.
8. **Documentación en español y sin guiones largos (U+2014) para incisos**: usar comas, paréntesis o dos puntos.
9. **Memoria del proyecto.** Al cerrar cada fase se registra en `progress.md` qué cambió, por qué y las mediciones. Las restricciones descubiertas van en `findings.md`.

## Arquitectura

El contrato técnico completo (módulos, parser, matcher, protocolo v2, audio, configuración) está en `docs/diseno_v2.md`. Resumen:

```
frontend/ (app.js, index.html, styles.css)  <-- WebSocket v2 -->  backend/server.py
backend/  config.py · protocol.py · voice.py · fakes.py · server.py
tools/    text_norm · script_parser · script_matcher · transcript_filter · audio_capture
          audio_file_source · vad_gate · stt_engine · cuda_dlls · replay · record_sample · download_model
```

Flujo de voz: captura continua (buffer circular 16 kHz) → VAD → Whisper sobre ventanas solapadas → filtro de alucinaciones → matcher por alineamiento → `scroll` con época.

## Cómo correr

- Entorno: `.venv312` (Python 3.12, sin torch). El antiguo `.venv` está roto y se conserva como respaldo.
- Backend en modo navegador: `.venv312\Scripts\python.exe -m uvicorn backend.server:app --host 127.0.0.1 --port 8765` y abrir `http://127.0.0.1:8765`.
- App nativa (pywebview, oculta en capturas): `scripts\start_stealth.bat`.
- Hito 84, doble clic en la raíz: `ABRIR_TELEPROMPTER.bat` (app nativa) o `ABRIR_EN_NAVEGADOR.bat` (backend + navegador en 8765). La tecla Prompter abre `scripts\start_stealth.vbs`; los tres usan `.venv312`.
- Controles externos (Stream Deck): WebSocket `ws://127.0.0.1:8765/control`, contrato T6 en `kit_Galleon100SD/docs/plan84/DECISIONES_84.md`. Clases y variables que comparte la lógica con `tema.css`: `frontend/CLASES.md`.
- Pruebas de extremo a extremo y capturas (Chrome headless, sin red): `tests\test_e2e_control.py`, `tools\capturas_v3.py`; sabotajes de las puertas del hito 84: `tools\sabotajes_84.py`.
- Tests (sin GPU, micrófono ni red): `.venv312\Scripts\python.exe -m pytest -q`.
- Replay de grabaciones: `.venv312\Scripts\python.exe tools\replay.py --wav tests\fixtures\audio\<archivo>.wav --script guiones\ejemplo.txt`.
- Grabar muestras: `.venv312\Scripts\python.exe tools\record_sample.py --name <nombre> --seconds 90`.
- Comandos del proyecto: `/test`, `/run`, `/replay`, `/qa`. Revisor previo a cada commit: agente `revisor-reglas`.

## Convenciones

- Commits en español, uno por fase o bloque lógico, en la rama `fix/pulido-integral`.
- Leer cada archivo completo antes de modificarlo y verificar las APIs contra la versión instalada en `.venv312\Lib\site-packages`.
- Variables de configuración `TP_*` centralizadas en `backend/config.py`; los lanzadores no las sobrescriben.
- CUDA sin torch: llamar a `tools.cuda_dlls.register_cuda_dlls()` antes de cargar un modelo en GPU.
