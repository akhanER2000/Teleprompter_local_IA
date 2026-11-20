# PROGRESS LOG

## 2026-05-15 · Inicialización B.L.A.S.T.
- Estructura de carpetas creada: `architecture/`, `tools/`, `backend/`, `frontend/`, `scripts/`, `logs/`, `guiones/`.
- Constitución `gemini.md` redactada con Data Schema congelado.
- Stack confirmado: Python 3.11 + FastAPI + faster-whisper + JS vanilla.

## Fase 1 · Blueprint
- Discovery cerrado: entrada = micrófono default, guion = textarea/paste, salida = WebSocket JSON.
- Schema de payload `scroll` definido (idx, matched_word, confidence, latency_ms).

## Fase 2 · Link
- `tools/check_cuda.py`: chequea torch.cuda + nombre de GPU.
- `tools/check_audio.py`: lista dispositivos de entrada.
- `tools/check_whisper.py`: carga `medium` en CUDA y transcribe 1 seg de silencio.

## Fase 3 · Architect
- SOPs escritos en `architecture/`.
- `audio_capture.py`: thread productor → cola de chunks 500 ms.
- `stt_engine.py`: consume cola, devuelve texto + timestamps.
- `script_matcher.py`: sliding window con normalización Unicode.
- `backend/server.py`: orquesta los tres módulos, expone `/ws`.

## Fase 4 · Stylize
- UI con tipografía 64px default, contraste alto (texto blanco sobre fondo negro).
- Textos por delante con opacidad reducida; palabra actual resaltada; texto leído atenuado.
- Smooth scroll vía `element.scrollIntoView({behavior: 'smooth', block: 'center'})`.

## Fase 5 · Trigger
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

## 2026-10-09 · Fase 1: sincronización correcta

**Qué cambió**
- `tools/text_norm.py` y `tools/script_parser.py`: el backend es la única fuente de verdad del guion. Produce segmentos `word`, `directive` y `break` y tokens con unidades de coincidencia (NFKD, sin tildes, números a palabras con `num2words`, grupos de miles). Los tokens sin unidades (guion largo, `...`, signos sueltos, emojis) conservan su índice y se marcan `skip`.
- Protocolo v2 (`backend/protocol.py`, `backend/server.py`): posiciones absolutas, épocas (`seq`), `start` atómico con índice, `set_position` con eco, conexión nueva que reemplaza a la anterior (código 4000), cola de salida única que deja solo el último `scroll` y `level`, validación de cada mensaje sin cerrar la conexión, estado de sesión que sobrevive a las reconexiones.
- `backend/voice.py`: bucle de voz con interfaz de captura nueva (buffer circular en `tools/audio_capture.py`, sin descartar audio), descarte de resultados obsoletos por generación y vigilancia `MIC_LOST`. Conserva la semántica de bloques de la versión anterior hasta la Fase 2.
- `tools/stt_engine.py`: `local_files_only`, error `MODEL_MISSING` claro, warmup, fallback a CPU `int8` y, en CPU, cambio automático de un `TP_COMPUTE` no admitido a `int8`.
- `tools/audio_file_source.py`: un WAV como micrófono en tiempo real (`TP_AUDIO_FILE`), para pruebas y demos.
- `frontend/`: dibuja los `segments` (párrafos, saltos de línea, directivas por rango), máquina de estados explícita (VOZ solo tras `listening`), reconexión con resincronización de guion y posición, avisos visibles también en modo lectura, indicadores de conexión y de modelo, un solo keymap que ignora campos de formulario, Auto-scroll sin depender del backend.
- `backend/config.py`: configuración centralizada (`TP_*`, `config.json` opcional, valores inválidos con advertencia).

**Por qué**: resuelve C1, C2, C4, C5, A1, A2, A3, A8, A9, M5, M6, M7 y M15. Además se adelantaron partes de otras fases porque la reescritura las tocaba igual: el lifespan con carga del modelo en segundo plano (núcleo de C6), `config.py` (Fase 3), clases incrementales y keymap único (M3, M4), warmup y fallback a CPU (M8, M16) y la vía de remuestreo de la captura (parte de M13).

**Revisión adversarial**: 5 revisores independientes (concurrencia, frontend, contrato, casos borde y prueba real de punta a punta) y un verificador por cada uno. Se confirmaron 22 hallazgos (unos 15 distintos) y se descartaron 3; todos se corrigieron con pruebas que los habrían detectado. El más grave: un surrogate UTF-16 suelto en el guion mataba en silencio el envío de la conexión y de cada reconexión hasta reiniciar el backend; ahora el texto se sanea y un envío fallido cierra con 1011 para que el cliente reconecte.

**Mediciones** (RTX 5070 Ti, `TP_MODEL_SIZE=medium`, `TP_DEVICE=cuda`, `TP_COMPUTE=float16`, `TP_STEP_MS=350` con bloques mínimos de 0,5 s, `TP_AUDIO_FILE` con la lectura sintética de `guiones/ejemplo.txt`)
- Pruebas: 387 pasan en ~11 s, sin GPU, micrófono ni red (antes de la fase: 12).
- Arranque: el puerto responde de inmediato; el modelo `medium` queda listo en 2,3 s (carga 1,5 s y warmup 0,5 s).
- Smoke test en el navegador con el modelo real y la lectura sintética como micrófono: Auto se detuvo en la palabra 12 y la voz continuó desde la 12 sin retroceder; avanzó de forma monótona hasta el final (89 de 89). Shift+← en voz dejó la posición en 74 sin rebotes. Al recargar la página se recuperaron guion, texto y posición 74. Al caer el backend aparece "Conexión perdida. Reconectando…" en modo lectura, la voz pasa a pausa y, al volver el backend sin guion, el cliente lo reenvía junto con la posición.
- Latencias observadas: `stt_ms` mediana ~104 ms (78 a 140 ms), `lag_ms` mediana ~170 a 190 ms (92 a 234 ms).

**Pendiente y riesgos**
- Con el matcher voraz y bloques sin solapamiento, una lectura natural todavía puede quedarse pegada (en una corrida se detuvo en la palabra 50 de 89 tras dos bloques vacíos). Es exactamente C3 y A4, y se resuelve en la Fase 2.
- Al cargar un guion que empieza con un token sin unidades (por ejemplo una viñeta con guion largo), el cursor inicial queda sobre ese token: es cosmético y se ajusta en la Fase 6.
- `faster_whisper.decode_audio` no funciona con PyAV 19 (la app no lo usa); la Fase 5 decide la versión fijada de `av`.

## 2026-11-20 · Hito 85: vista con referencias (R1 a R6)

**Qué cambió**
- Parser: `[img: …]`, `[ppt: …#N]`, `[pdf: …#N]`, `[code: …#L10-L25]` y `[nota: …]` son segmentos `ref` (tipo, archivo, fragmento, etiqueta y `at_idx`). No dan tokens ni son secciones; una referencia sola en su renglón no cambia los saltos del texto.
- Servidor: `GET /api/ref` (`backend/refs.py`) resuelve la ruta (`guiones/<guion>_recursos/`, `guiones/`, absoluta), dibuja y cachea la miniatura en `guiones/.cache/` (`tools/ref_thumbs.py`, `tools/slides_engine.py`) o colorea el código (`tools/code_highlight.py`). Fallos: 404 «No encontrado: nombre»; sin motor de diapositivas, 501 `SIN_MOTOR`.
- Frontend: vistas Solo texto y Con referencias (tecla V, orden `view`, configuración, recordada), tarjetas al costado del renglón, tamaño, opacidad, anuncio previo, Ampliar (tecla Z, orden `ref_zoom`), «Sobre presentación» con las referencias al 85 %.
- /control: órdenes `view` y `ref_zoom`; estado con `vista`, `ref_actual` y `ref_ampliada`.
- Dependencias nuevas fijadas: pillow 11.0.0, pypdfium2 4.30.0; Pygments 2.21.0 pasa a directa.

**Por qué**: pedido del 20-11-2026 (DECISIONES_85.md).

**Mediciones**: 728 pruebas pasan (694 + 31 nuevas + 3 de test_entorno por las dependencias fijadas). 17 sabotajes en rojo (`tools/sabotajes_85.py`). Diapositiva con LibreOffice: 1,9 s la primera vez, luego desde la caché. Detalle en `docs/hito85/README.md`.
