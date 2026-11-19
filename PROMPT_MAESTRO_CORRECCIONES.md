# PROMPT MAESTRO · Pulido integral de Teleprompter Local IA ("Flow Local")

> **Instrucción para Claude Code:** lee este documento completo antes de tocar código. Ejecuta el plan de la sección 7 por fases y en orden, con al menos un commit por fase. Cada hallazgo de la sección 4 fue verificado leyendo el código y, cuando se indica, reproducido ejecutándolo; aun así, vuelve a verificarlo antes de corregir. Si alguno resulta incorrecto, dilo y no lo "arregles" a la fuerza.

---

## 1. Rol y objetivo

Actúa como ingeniero de software senior con experiencia en audio en tiempo real, faster-whisper/CTranslate2, FastAPI con WebSockets, frontend vanilla y aplicaciones de escritorio Windows (pywebview, Edge WebView2, Win32).

**Objetivo:** dejar el teleprompter completamente pulido y confiable para uso real (presentaciones, grabaciones y videollamadas) sin perder nada de lo que hoy funciona. Prioridades, en este orden:

1. Sincronización por voz correcta y estable: sin saltos hacia atrás, sin quedarse pegado y sin desfases entre lo resaltado y lo leído.
2. Arranque y app nativa robustos: sin cuelgues silenciosos, sin procesos huérfanos y con errores visibles.
3. Privacidad real "100% local".
4. Entorno reproducible y liviano.
5. UX pulida.
6. Documentación fiel al código.

---

## 2. Contexto del proyecto (estado verificado el 2026-10-09)

- **Ruta:** `J:\Code\Teleprompter_local_IA`. Repo git limpio con 2 commits ("Initial commit con Auto-scroll y Directivas" e "Implementación de Modo Stealth, Always on Top y ventana nativa con pywebview").
- **Hardware objetivo:** Windows 10/11, NVIDIA RTX 5070 Ti (16 GB, arquitectura Blackwell, sm_120).
- **Stack:** FastAPI 0.115 + Uvicorn 0.30 (WebSocket en `ws://127.0.0.1:8765/ws`); faster-whisper 1.0.3 sobre CTranslate2 4.7.1 en CUDA float16, modelo `medium`, idioma `es`; sounddevice 0.4.7; frontend HTML/CSS/JS vanilla sin build; ventana nativa con pywebview 6.2.1 (Edge WebView2) y exclusión de captura con `SetWindowDisplayAffinity`.
- **Entorno actual:** `.venv` creado con **Python 3.9.13** (el Python compartido de Visual Studio). Pesa 4,7 GB, de los cuales 4,3 GB son `torch 2.5.1+cu121`, que solo usa `tools/check_cuda.py`.
- **Flujo:** `frontend/app.js` parsea el guion y lo envía por WebSocket; `backend/server.py` orquesta `tools/audio_capture.py` (bloques de 500 ms) → `tools/stt_engine.py` (Whisper) → `tools/script_matcher.py` (ventana deslizante) y devuelve mensajes `scroll` con el índice de palabra.
- **Documentos de diseño:** `gemini.md` (constitución y esquema de datos), `architecture/SOP_01..03`, `findings.md`, `progress.md`, `task_plan.md`.
- **Lanzadores:** `scripts/install.bat`, `scripts/start.bat` (modo navegador), `scripts/start_stealth.bat` → `start_stealth.vbs` → `pythonw launch.py` (app nativa), `scripts/handshake.bat`.

---

## 3. Reglas de trabajo

1. Crea la rama `fix/pulido-integral` desde la rama actual. Un commit por fase (o por bloque lógico dentro de una fase), con mensajes en español.
2. Lee cada archivo completo antes de modificarlo. No asumas APIs: verifica contra la versión instalada de cada librería (código fuente en `site-packages` o documentación de esa versión).
3. No rompas lo que funciona. Al cerrar cada fase corre los tests y un smoke test real: arrancar el backend, cargar `guiones/ejemplo.txt` y probar Auto-scroll y Manos libres.
4. **Principio innegociable:** ejecución 100% local. Ninguna petición de red en tiempo de ejecución (ni fuentes, ni CDN, ni telemetría). La red solo se usa al instalar dependencias y descargar el modelo durante `install.bat`.
5. El frontend sigue siendo HTML/CSS/JS vanilla sin build step. No introduzcas frameworks.
6. Respeta la atomicidad de la constitución: cada módulo de `tools/` hace una sola cosa y la composición vive en `backend/`.
7. Agrega dependencias solo si están justificadas y siempre con versión fijada (por ejemplo `pytest`, `num2words`).
8. No borres `.venv`, guiones ni archivos míos sin preguntarme. Para cambios de entorno, crea uno nuevo en paralelo y deja el anterior como respaldo.
9. Documentación en español y **sin guiones largos (—) para incisos**: usa comas, paréntesis o dos puntos.
10. Al cerrar cada fase, registra en `progress.md` qué cambió, por qué y las mediciones obtenidas.
11. Las decisiones de la sección 6 requieren mi confirmación antes de implementarlas. Mientras tanto, avanza con todo lo demás.
12. Trabajo en Windows: usa `.venv\Scripts\python.exe` para ejecutar Python y pytest.

---

## 4. Diagnóstico (hallazgos priorizados)

Severidad: **C** crítico (rompe la función principal), **A** alto, **M** medio, **B** bajo.

### 4.1 Críticos

**C1. Frontend y backend tokenizan distinto, así que el resaltado se desfasa.**
`frontend/app.js:68-101` separa por espacios y cuenta todo token, incluidos `—`, `...`, `¿` o `?` sueltos. `tools/script_matcher.py:23-32` descarta los tokens cuya normalización queda vacía. Los índices dejan de coincidir y el `current_idx` del backend apunta a otra palabra en el frontend.
*Reproducido:* para el texto `Hola — mundo. Esto es la RTX-5070 ... y el sistema ¿funciona ? sí` el frontend genera 14 tokens y el backend 11. Al final de la frase el resaltado va 3 palabras atrasado, y el error se acumula en guiones largos.

**C2. Al pasar de Auto-scroll (o de las flechas) a Manos libres, el texto salta hacia atrás.**
El auto-scroll (`app.js:275-288`) mueve la posición solo en el frontend y nunca la informa al backend. Al pulsar Manos libres (`app.js:360-366`) el backend conserva su `current_idx` antiguo y el primer `scroll` devuelve el texto a esa posición. Las flechas tienen el mismo defecto: el frontend aplica "su índice + delta" y el backend (`server.py:166-171`, acción `step`) aplica "su índice + delta", y ambos divergen.

**C3. El audio se transcribe en bloques independientes de 500 ms, sin solapamiento.**
`tools/audio_capture.py:34-45` corta bloques de 8000 muestras y `tools/stt_engine.py:35-49` transcribe cada bloque aislado. Las palabras quedan partidas en los bordes y Whisper trabaja casi sin contexto. El texto se queda pegado y luego salta.
*Reproducido con el matcher real:* la secuencia de bloques `"En la fa"`, `"se dos demos"`, `"traremos el funcio"`, `"namiento del"`, `"módulo de recono"`, `"cimiento de voz"` deja el índice en 0 durante 4 bloques y luego avanza a saltos de 6 palabras (el tope por bloque).

**C4. Después de reposicionar llegan mensajes `scroll` obsoletos (condición de carrera).**
En `server.py:118-140`, si el worker está transcribiendo audio anterior cuando el usuario reinicia o mueve la posición, ese resultado se aplica después y devuelve el texto a la posición vieja. `_drain_audio_queue` y el modo "sticky" solo lo mitigan. No existe un número de época para descartar resultados viejos.

**C5. Los errores son invisibles en modo lectura y el estado queda incoherente.**
El único lugar donde se muestran mensajes es `#status` (`index.html:15`, `app.js:163`), que está dentro del panel de configuración y se oculta en modo lectura. Si el micrófono falla o no hay guion, el backend envía `status: error`, pero `handleMessage` (`app.js:138-160`) no revierte `state.mode`. La UI queda en "🎙 VOZ" sin escuchar y sin mostrar por qué.

**C6. El arranque es frágil porque el modelo se carga antes de abrir el puerto.**
`server.py:189-191` carga Whisper en `startup`, y Uvicorn no acepta conexiones hasta que termina. `start.bat:18` abre el navegador de inmediato, así que aparece una página de "no se puede conectar" que no se reintenta sola (el JS todavía no existe). `launch.py:84-92` y `238-245` esperan 60 s: en la primera ejecución (descarga del modelo, unos 1,5 GB para `medium` y 3 GB para `large-v3`) aparece "No se pudo iniciar el backend" sin indicar la causa, mientras el hilo sigue descargando.

### 4.2 Altos

**A1. Las directivas consecutivas se clasifican mal.** `app.js:210-229` calcula el estado de cada directiva con los hermanos del DOM. *Reproducido:* con `[Cambiar a slide 2] [Mostrar grafico]` seguidas, la segunda queda "actual" desde la primera palabra del guion y la primera queda "actual" hasta el final.

**A2. Se pierden los saltos de párrafo.** `parseScript` separa por `\s+` y el lector muestra un bloque continuo (*reproducido*). Para un teleprompter, los párrafos son clave para la legibilidad y las pausas.

**A3. Las palabras con guion o barra nunca coinciden.** `normalize()` (`script_matcher.py:8-13`) convierte `RTX-5070` en `"rtx 5070"` como un único token. Se compara contra palabras sueltas del transcript, así que nunca coincide (*reproducido*). Lo mismo ocurre con `uno/dos`, `e-mail`, etc. Los números también fallan cuando el guion los escribe en dígitos y Whisper los transcribe en palabras, o al revés.

**A4. El matcher es frágil.** Toma la primera coincidencia de forma voraz, usa `LOOKBEHIND = 3` y `LOOKAHEAD = 12`, y no se recupera si el orador se salta un párrafo o retrocede para releer una frase (se queda pegado hasta que se use una flecha). El estado "desincronizado" está documentado en SOP_02, pero no implementado.

**A5. No se filtran las alucinaciones de Whisper en español.** Frases como "Subtítulos realizados por la comunidad de Amara.org", "¡Gracias por ver!" o "Suscríbete" aparecen ante ruido o silencio y pueden hacer avanzar el texto cerca de un cierre que diga "muchas gracias". Tampoco se usan `no_speech_prob` ni `avg_logprob` de cada segmento.

**A6. El umbral RMS fijo descarta audio.** `audio_capture.py:37-39` (umbral 0.005) elimina bloques completos: con un micrófono de baja ganancia se pierden los inicios de palabra. No hay medidor de nivel ni calibración.

**A7. Seguridad: el WebSocket no valida el `Origin`.** `server.py:194-211`. Cualquier página web abierta en el navegador puede conectarse a `ws://127.0.0.1:8765/ws` cuando la app no tiene cliente, iniciar el micrófono y recibir los mensajes `transcript` con lo que el usuario dice (los WebSockets no están sujetos a CORS). Esto contradice la constitución de privacidad.

**A8. La política de cliente único rechaza las reconexiones legítimas.** `server.py:61-68`: al recargar la página o abrir otra pestaña, la conexión nueva se rechaza si la anterior aún no se liberó, y la pestaña rechazada reintenta indefinidamente.

**A9. La reconexión del frontend no restaura el estado.** `app.js:120-125`: `state.mode` queda en `voice` y `scriptLoaded` sigue en `true`, aunque el backend se haya reiniciado sin guion.

**A10. En modo stealth no hay ningún diagnóstico.** `launch.py:38-44` silencia todo el logging y `run_server` (`launch.py:75-76`) se traga las excepciones. La carpeta `logs/` existe, pero no se usa.

**A11. Doble instancia y procesos huérfanos.** Si `start_stealth` se lanza dos veces, se carga un segundo modelo en VRAM y el bind del puerto falla en silencio. Además, `wait_for_backend` se conecta al puerto de la primera instancia y abre una ventana que reintenta para siempre. Un `pythonw` huérfano e invisible bloquea el puerto en el siguiente arranque.

**A12. Los lanzadores pisan la configuración.** `start.bat:12-15` y `start_stealth.vbs:35-38` sobrescriben las variables `TP_*`, así que lo que el README dice que se puede configurar (por ejemplo `set TP_MODEL_SIZE=small`) no tiene efecto.

**A13. El entorno está desactualizado y sobredimensionado.** Python 3.9 está fuera de soporte desde octubre de 2025, y el README, `gemini.md` e `install.bat` se contradicen (3.9-3.11, 3.11 y 3.11+). Además, `torch 2.5.1+cu121` no trae kernels para sm_120: las RTX serie 50 requieren builds con CUDA 12.8 o superior, así que `check_cuda.py` da un resultado engañoso, y torch ocupa 4,3 GB solo para ese chequeo.

**A14. Las fuentes se cargan desde Google Fonts.** `index.html:7-9` y `styles.css:1` (doble carga, con `@import`, que bloquea el render): se hace una petición externa en cada arranque, lo que contradice "100% local" y falla sin conexión.

### 4.3 Medios

- **M1.** `scrollIntoView` con animación suave en cada actualización reinicia la animación y produce temblor. Además, `behavior: "auto"` hereda `scroll-behavior: smooth` de `styles.css:195`, por lo que reiniciar anima el desplazamiento por todo el guion.
- **M2.** El auto-scroll avanza una palabra cada intervalo fijo en vez de desplazarse de forma continua: el texto salta de línea en línea y no considera la longitud de las palabras ni las pausas de la puntuación.
- **M3.** `applyScroll` (`app.js:203-251`) recorre todo el DOM en cada actualización (O(n)). En guiones largos produce tirones.
- **M4.** Los atajos solo ignoran `TEXTAREA` (`app.js:405`). Con el foco en un slider, Espacio, las letras y las flechas disparan acciones. Además hay dos listeners `keydown` separados (`app.js:404` y `569`).
- **M5.** El Auto-scroll exige que el backend confirme el guion, aunque no lo necesita.
- **M6.** Acoplamiento frágil: `scriptLoaded` se activa buscando el texto "Guion cargado" dentro del mensaje (`app.js:144`).
- **M7.** `send()` (`app.js:132-136`) descarta mensajes en silencio si el WebSocket no está abierto: "Cargar guion" sin conexión no da ningún aviso.
- **M8.** No hay warmup tras cargar el modelo, por lo que la primera transcripción es lenta.
- **M9.** pywebview: `api.window = window` (`launch.py:265`) deja el objeto `Window` como atributo público del puente JS. El topmost se aplica con `SetWindowPos` desde un hilo que no es el de la GUI, y el código ya documenta congelamientos relacionados. El HWND se busca por título con `FindWindowW` (`launch.py:114-117`). F11 usa `requestFullscreen` en lugar del fullscreen nativo de la ventana.
- **M10.** El nombre "Stealth" está mal usado y mal documentado: la tecla L solo activa la exclusión de captura, pero el README promete "bloquear la interacción para evitar clics accidentales" y "transparencia", y ninguna de las dos cosas existe.
- **M11.** `install.bat` no verifica errores de pip y termina diciendo "Instalación completa" aunque algo haya fallado. Además, instala `pywebview[edgechromium]` sin versión después del requirements (puede romper el pin y ese extra no existe), no descarga el modelo por adelantado y usa el `python` que esté primero en el PATH (por eso el entorno terminó en 3.9).
- **M12.** `requirements.txt` no fija dependencias transitivas críticas (`ctranslate2`, `onnxruntime`, `huggingface_hub`, `tokenizers`, `av`), y `python-multipart` no se usa.
- **M13.** No se puede elegir el dispositivo de entrada y no se remuestrea si el dispositivo no acepta 16 kHz (`findings.md` lo exige, pero no está implementado).
- **M14.** Nada es persistente: el guion y los ajustes (tamaño, interlineado, velocidad) se pierden al cerrar. pywebview arranca en modo privado por defecto (verificarlo en 6.2.1). `guiones/` existe, pero la UI no permite abrir archivos de ahí.
- **M15.** `int(msg.get("idx"))` (`server.py:161`) sin validación puede lanzar una excepción y cerrar la conexión. `load_script` durante la escucha no detiene ni resincroniza el pipeline. Varias tareas envían por el mismo WebSocket sin una cola de salida única.
- **M16.** No hay fallback a CPU si CUDA falla.

### 4.4 Bajos y documentación

- **B1.** README: a la tabla de atajos le faltan ← y →, L y T están mal descritos y la versión de Python es contradictoria.
- **B2.** `gemini.md` y los SOP están desalineados con el código: la acción `step` no está documentada; SOP_02 dice `LOOKAHEAD 25` y el código usa 12; el heartbeat, el estado "desincronizado" y la descarga de mensajes `scroll` viejos están documentados, pero no implementados. Los KPI aparecen como cumplidos, aunque todas las pruebas de latencia de `progress.md` siguen pendientes.
- **B3.** `check_whisper.py` fija `medium` y CUDA e ignora la configuración. El docstring dice 2 s, pero graba 3 s.
- **B4.** El overlay de "latencia" muestra solo el tiempo de inferencia, no la latencia extremo a extremo.
- **B5.** No hay ningún test automatizado.
- **B6.** Hay código muerto o que hay que revisar: `server.install_signal_handlers = lambda: None` (comprobar si Uvicorn 0.30 lo sigue necesitando), `quit_app` no se usa y conviene revisar el fallback a `WDA_MONITOR`.

---

## 5. Arquitectura objetivo (resumen)

1. **Una sola fuente de verdad para el guion:** el backend parsea el texto (directivas, párrafos, tokens) y envía los `segments` al frontend, que solo dibuja. Cada token visible tiene un índice 1:1 con lo que se muestra y una lista interna de **unidades de coincidencia** normalizadas (0..n): `RTX-5070` → `["rtx", "cinco", "mil", "setenta"]`, `—` → `[]`. Los tokens sin unidades se saltan solos.
2. **Protocolo v2** (sección 8): posiciones absolutas, una época (`seq`) definida por el cliente para descartar resultados viejos, mensajes de estado explícitos y una cola de salida única.
3. **Captura continua con buffer circular** (los últimos 3 s aprox.), detección de voz para decidir cuándo transcribir y ventanas solapadas cada 300-400 ms, sin acumular retraso.
4. **Matcher v2:** alineamiento local por programación dinámica sobre unidades, ponderado (las palabras de contenido pesan más que las stopwords), con resincronización automática en un rango amplio.
5. **Arranque inmediato:** el servidor abre el puerto al instante y el modelo carga en segundo plano informando su estado. Configuración centralizada, logging a archivo, instancia única y cierre limpio.
6. **Herramienta de replay** para medir y ajustar la sincronización con grabaciones reales, sin tener que hablar en cada prueba.

**Mapa de archivos esperado** (orientativo):
```
backend/  server.py · config.py · protocol.py (esquemas y validación)
tools/    audio_capture.py · vad_gate.py · stt_engine.py · script_parser.py · script_matcher.py
          text_norm.py · replay.py · record_sample.py · download_model.py · check_*.py
frontend/ index.html · app.js · styles.css · fonts/ (Inter woff2 + licencia OFL)
tests/    test_parser.py · test_matcher.py · test_protocol.py · test_api.py · fixtures/
.claude/  settings.json · commands/ · rules/ · agents/ · skills/
CLAUDE.md · CLAUDE.local.md (ignorado por git) · data/ (ignorado) · logs/ (ignorado)
```

---

## 6. Decisiones que requieren mi confirmación

Antes de implementar cada una, preséntame las opciones con tu recomendación y espera mi respuesta. Mientras tanto, sigue con el resto del plan.

- **D1. Entorno:** recrear el entorno con Python 3.11, sin torch (se liberan unos 4,3 GB). Plan propuesto: crear un venv nuevo en paralelo, verificarlo completo y dejar el actual como respaldo hasta que yo confirme.
- **D2. Modelo por defecto:** actualizar faster-whisper a una versión 1.1.0 o superior (agrega soporte para `large-v3-turbo`) y cambiar el modelo por defecto solo si el replay muestra una mejora real de precisión o latencia frente a `medium`.
- **D3. Bloqueo de interacción:** implementar uno real (ventana que deja pasar los clics, `WS_EX_TRANSPARENT`, con un atajo global `RegisterHotKey` para desbloquear, porque la ventana ya no recibiría el teclado) o eliminarlo de la documentación.
- **D4. Transparencia u opacidad de la ventana:** hacer una prueba de viabilidad con WebView2. Si funciona bien, agregar un control de opacidad; si no, eliminar la mención del README.
- **D5. `gemini.md`:** convertirlo en un puntero breve a `CLAUDE.md` o mantener ambos sincronizados.

---

## 7. Plan de ejecución por fases

### Fase 0 · Preparación y línea base
1. Crea la rama `fix/pulido-integral`.
2. Registra la línea base en `progress.md`: versión de Python, `pip freeze` (guárdalo en `docs/baseline_freeze.txt`) y el resultado de `scripts\handshake.bat`.
3. Crea `tools/record_sample.py` (graba N segundos del micrófono a un WAV de 16 kHz mono en `tests/fixtures/audio/`, carpeta ignorada por git porque contiene mi voz). Luego **pídeme** que grabe al menos tres lecturas de `guiones/ejemplo.txt`: una a ritmo normal, otra saltándome un párrafo y releyendo una frase, y 15 s de silencio o ruido ambiente.
4. Instala `pytest` (versión fijada) en el venv actual y crea `tests/` con un `conftest.py`.
5. Monta el centro de control `.claude/` del proyecto:
   - `CLAUDE.md`: propósito, constitución actualizada (las reglas de la sección 3), arquitectura, protocolo, cómo correr, testear y hacer replay.
   - `CLAUDE.local.md` (ignorado por git): datos de mi máquina (RTX 5070 Ti, rutas de Python y del venv).
   - `.claude/settings.json`: permitir pytest, Python del venv y comandos git de lectura y commit; pedir confirmación para borrar archivos o instalar paquetes. Agrega también un `settings.local.json` ignorado por git.
   - `.claude/commands/`: `/test`, `/run`, `/replay` y `/qa` (este último recorre la checklist de la sección 10).
   - `.claude/rules/`: `privacidad-local.md`, `frontend-vanilla.md` y `docs-es.md` (incluye la regla de no usar guiones largos).
   - `.claude/agents/`: un revisor que compruebe las reglas antes de cada commit.
   - `.claude/skills/`: revisa `J:\Code\Claude_Skills\CATALOG.md` y copia **solo** las skills relevantes (backend/FastAPI, frontend, testing, seguridad, documentación y audio/ML si existen).
6. Actualiza `.gitignore`: `.venv*/`, `models/`, `data/`, `logs/`, `tests/fixtures/audio/`, `CLAUDE.local.md` y `.claude/settings.local.json`.

### Fase 1 · Sincronización correcta (C1, C2, C4, C5, A1, A2, A3, A8, A9, M5, M6, M7, M15)
1. **Parser único en el backend** (`tools/script_parser.py`). Genera:
   - segmentos `word`, `directive` y `break` (párrafo o línea);
   - directivas `[...]`, incluidas las de varias líneas;
   - cada palabra con su índice y sus unidades normalizadas, separadas por cualquier carácter no alfanumérico (con la normalización de `tools/text_norm.py`: NFD, sin tildes, minúsculas y números a palabras en la Fase 2);
   - tokens sin unidades que se conservan en el índice pero se marcan para saltarse.

   Casos borde con test: corchete sin cerrar (se trata como texto), corchetes anidados (define la regla y pruébala), directiva al inicio o al final, directivas consecutivas, CRLF, tabs, espacios múltiples y emojis.
2. **El frontend dibuja los `segments`** que llegan en `script_loaded` y se elimina `parseScript` de `app.js`. Los párrafos se dibujan como bloques. Cada directiva se clasifica con su `at_idx` (índice de la primera palabra que la sigue): leída si `current_idx > at_idx`, actual si `current_idx == at_idx` y próxima en el resto de los casos. Nunca con los hermanos del DOM.
3. **Implementa el protocolo v2** (sección 8): posiciones absolutas, `start` con índice (atómico), `seq`/época y una cola de salida única que, si se acumulan mensajes `scroll`, envía solo el último. La conexión nueva reemplaza a la anterior. Valida tipos y rangos de cada mensaje: un payload inválido responde un error y **nunca** cierra la conexión. `load_script` durante la escucha pausa, recarga y resincroniza.
4. **Máquina de estados explícita en el frontend:** la UI pasa a "VOZ" solo al recibir `status: listening`. Ante un error vuelve a pausa y lo muestra. Al cerrarse el WebSocket pasa a pausa con "Reconectando…", y al reconectar envía `hello`, compara `script_id` y, si hace falta, reenvía el guion y la posición.
5. **Notificaciones visibles en modo lectura:** una barra o toast arriba, y un indicador de conexión y estado del modelo en el overlay. Todo `status` de error se ve ahí.
6. El Auto-scroll solo requiere que el guion esté cargado, no el modelo.
7. Si el WebSocket no está abierto, `send()` avisa al usuario (y encola solo las acciones idempotentes).

**Aceptación:** pasan los tests de parser y protocolo. Pasar de Auto (posición 120) a Voz continúa en 120 o más. Los `scroll` de una época anterior se ignoran. Un error de micrófono muestra un aviso y devuelve el modo a pausa. Recargar la página no deja el teleprompter bloqueado. Un guion con `—`, `...`, `RTX-5070`, directivas consecutivas y párrafos se dibuja y resalta correctamente.

### Fase 2 · Motor de voz (C3, A4, A5, A6, M8, M13, M16)
1. **Captura continua** (`audio_capture.py`):
   - buffer circular con los últimos `TP_WINDOW_S` segundos (3,0 por defecto) y un contador global de muestras;
   - nunca descarta audio por RMS;
   - calcula el nivel en dBFS cada ~100 ms para el medidor;
   - abre a 16 kHz y, si el dispositivo lo rechaza, abre a su tasa nativa y remuestrea (PyAV ya está instalado por faster-whisper: `av.AudioResampler`);
   - permite elegir el dispositivo con `TP_INPUT_DEVICE` y listarlos vía API.
2. **Bucle de inferencia:**
   - cada `TP_STEP_MS` (300-400 ms por defecto) toma la ventana;
   - con Silero VAD (`tools/vad_gate.py`, que reutiliza `faster_whisper.vad`) revisa si hubo voz en el último segundo y, si no la hubo, no transcribe;
   - si hubo voz, transcribe con `beam_size=1`, `condition_on_previous_text=False`, `without_timestamps=True` e idioma fijo;
   - nunca lanza una inferencia si la anterior no terminó (se salta el ciclo; no se acumula retraso).

   Evalúa con el replay el uso de `initial_prompt` con las ~20-30 palabras del guion **anteriores** a `current_idx` (contexto previo, que es el uso para el que Whisper fue entrenado), activable con `TP_PROMPT_CONTEXT`.
3. **Filtro de alucinaciones:**
   - descarta segmentos con `no_speech_prob > 0.6` y `avg_logprob < -1.0`;
   - mantén una lista negra normalizada de frases típicas en español ("subtitulos realizados por la comunidad de amara org", "gracias por ver", "suscribete", "subtitulos por", etc.) y aplícala cuando el transcript consista solo en eso;
   - ignora los transcripts de una sola stopword.
4. **Matcher v2** (alineamiento por unidades):
   - alineamiento local por programación dinámica (tipo Smith-Waterman) entre las palabras del transcript (normalizadas igual) y las unidades del guion en la ventana de tokens `[current_idx - 10, current_idx + 25]`;
   - puntajes iniciales, a ajustar con el replay: palabra de contenido exacta +2, stopword exacta +0,5, coincidencia difusa (Levenshtein ≤ 1 en palabras de más de 3 letras) +1,2, sustitución -1, hueco -0,6;
   - acepta el avance solo si el puntaje supera el umbral, hay al menos una palabra de contenido alineada y el alineamiento termina dentro de las últimas 2-3 palabras del transcript (así se sigue la cola de lo dicho y la ventana solapada no vuelve a contar palabras viejas);
   - `new_idx = último token alineado + 1`, monótono, con un tope de avance por ciclo, saltando los tokens sin unidades.
5. **Resincronización:** si durante K ciclos con voz (6 por defecto) no se acepta ningún avance, emite `status: desync` y busca en `[current_idx - 40, current_idx + 200]` una coincidencia fuerte (4 o más palabras de contenido alineadas de forma consecutiva). Si la encuentra, salta ahí (hacia adelante o hacia atrás) y emite `scroll` con `resync: true`. Retroceder solo es posible por esta vía.
6. **Números:** `text_norm.py` convierte dígitos a palabras en español con `num2words`, tanto en las unidades del guion como en el transcript, para que "5070" y "cinco mil setenta" coincidan.
7. **Motor STT:**
   - warmup con 1 s de silencio después de cargar el modelo;
   - fallback a CPU `int8` si CUDA falla, avisando en la UI;
   - `local_files_only=True` en tiempo de ejecución, con un mensaje claro si falta el modelo;
   - reporta `stt_ms` y `lag_ms` (desde el fin del audio de la ventana hasta el envío del `scroll`).
8. **Replay** (`tools/replay.py --wav … --script … [--ref …]`):
   - alimenta el pipeline real (buffer, VAD, STT y matcher) simulando tiempo real a partir del WAV (`faster_whisper.decode_audio`);
   - exporta un CSV con la línea de tiempo y calcula las métricas;
   - referencia: alineamiento offline del WAV completo con `word_timestamps=True` contra el guion, usando el mismo alineador;
   - métricas: retraso p50 y p95 entre que se dice una palabra y que el índice la pasa, saltos falsos (más de 5 palabras), retrocesos, cobertura del guion y tiempo de resincronización tras saltarse un párrafo.
9. Compara configuraciones (`TP_STEP_MS`, `TP_WINDOW_S`, con y sin `initial_prompt`, y modelos si se aprueba D2) y registra la tabla en `progress.md`.

**Objetivos iniciales:** retraso p50 ≤ 700 ms y p95 ≤ 1500 ms; cero saltos falsos y cero retrocesos en las grabaciones de prueba; resincronización en 3 s o menos tras saltarse un párrafo; ningún avance con 15 s de silencio o ruido. Si las mediciones demuestran que la meta de "< 300 ms" de la constitución no es alcanzable con este enfoque, actualiza la constitución con la cifra real medida.

### Fase 3 · Arranque, app nativa y diagnóstico (C6, A10, A11, A12, M9, M10, B6)
1. **Lifespan de FastAPI** en lugar de `on_event`: el servidor abre el puerto de inmediato y carga el modelo en segundo plano. El estado del modelo (`loading`, `ready` o `error`) se informa por WebSocket, y Manos libres se habilita solo con `ready`. Al cerrar se detienen la captura y el worker.
2. **`backend/config.py`** como única fuente de configuración: variables `TP_*` con valores por defecto y un `config.json` opcional. Los `.bat` y el `.vbs` dejan de sobrescribir variables.
3. **Logging** a un archivo rotativo `logs/teleprompter.log` en ambos modos (en stealth sin consola, pero con archivo). `sys.excepthook` y `threading.excepthook` registran las excepciones no capturadas. `run_server` deja de tragarse excepciones y, si el backend no arranca, la ventana muestra la causa real y la ruta del log.
4. **Instancia única** con un mutex con nombre (`CreateMutexW`): si ya hay una instancia, se trae su ventana al frente y se sale. Se verifica que el puerto esté libre antes de arrancar y, si lo ocupa otro proceso, se muestra un error claro.
5. **Cierre limpio:** al cerrar la ventana, `server.should_exit = True` y se espera el hilo con timeout. Hay que garantizar que `pythonw` termine y libere el puerto (comprobarlo en el Administrador de tareas).
6. **pywebview** (verifica cada API en 6.2.1):
   - la ventana se guarda como atributo privado (`_window`) para que no se exponga al puente JS;
   - el topmost se cambia con la propiedad `window.on_top`, que se ejecuta en el hilo de la GUI, en vez de `SetWindowPos` desde otro hilo;
   - el HWND se obtiene del handle nativo de la ventana (o con `EnumWindows` filtrado por PID), no por título;
   - F11 usa `window.toggle_fullscreen()` vía API JS.
7. La ventana aparece de inmediato con el estado "Cargando modelo…" en vez de esperar en blanco.
8. **Nombres honestos:** "Stealth" pasa a llamarse "Oculto en capturas" (L) y se mantiene "Siempre adelante" (T). El bloqueo de interacción y la transparencia dependen de D3 y D4.

### Fase 4 · Seguridad y privacidad (A7, A14)
1. Valida el `Origin` en `/ws`: acepta solo `http://127.0.0.1:<puerto>` y `http://localhost:<puerto>` (comprueba qué `Origin` envía WebView2 en la app nativa). Rechaza el resto con el código 1008 y regístralo en el log.
2. Los mensajes `transcript` solo se envían con `TP_DEBUG=1`.
3. Fuentes locales: Inter en `frontend/fonts/` (woff2, con su licencia OFL) o una pila de fuentes del sistema. Elimina la doble carga.
4. El modelo funciona offline en tiempo de ejecución (`local_files_only=True`, `HF_HUB_OFFLINE=1`) y solo se descarga durante la instalación.
5. La API de guiones (Fase 6) se protege contra path traversal: solo nombres simples dentro de `guiones/`, extensiones `.txt` y `.md`, y un tamaño máximo.
6. Se mantiene el bind en `127.0.0.1` (nunca `0.0.0.0`).

**Aceptación:** un test verifica que una conexión con un `Origin` ajeno se rechaza. Sin `TP_DEBUG` no se envía ningún `transcript`. En DevTools no aparece ninguna petición externa.

### Fase 5 · Entorno e instalación (A13, M11, M12, B3), sujeta a D1
1. Crea el venv `.venv311` con Python 3.11 (`py -3.11 -m venv .venv311`). Si 3.11 no está instalado, avísame; no instales Python a nivel de sistema por tu cuenta.
2. Sin torch: `tools/check_cuda.py` pasa a usar `ctranslate2.get_cuda_device_count()`, `ctranslate2.get_supported_compute_types("cuda")` y `nvidia-smi` (nombre, VRAM y driver).
3. Verifica que CTranslate2 cargue en CUDA sin torch. Si faltan DLL de cuBLAS o cuDNN, instala `nvidia-cublas-cu12` y `nvidia-cudnn-cu12` en versiones compatibles con la CTranslate2 instalada y registra sus carpetas `bin` con `os.add_dll_directory` al iniciar.
4. Deja en `requirements.txt` las dependencias directas fijadas y genera `requirements.lock.txt` con `pip freeze`. Quita `python-multipart` si no se usa y elimina la línea `pywebview[edgechromium]` de `install.bat`.
5. `install.bat`:
   - detecta Python 3.11 con `py`;
   - se detiene con un mensaje claro ante cualquier error (`if errorlevel 1`);
   - descarga por adelantado el modelo configurado (`tools/download_model.py`), mostrando el progreso;
   - termina corriendo el handshake.
6. `check_whisper.py` usa la configuración central y corrige su texto.
7. Cuando todo pase, actualiza los lanzadores al nuevo entorno, deja el viejo renombrado como `.venv39_backup` y dime cuánto espacio libero si lo borro.

### Fase 6 · UX y pulido (M1, M2, M3, M4, M14)
1. **Scroll suave propio con `requestAnimationFrame`:** se acerca al objetivo de forma exponencial y no reinicia la animación si la línea objetivo no cambió. Al cargar o reiniciar, el salto es instantáneo. Quita `scroll-behavior: smooth` del CSS.
2. **Auto-scroll continuo** en píxeles por segundo, con la velocidad mostrada en palabras por minuto aproximadas y ajuste fino con ↑ y ↓. El resaltado sigue la línea de lectura y se detiene solo al llegar al final.
3. **Actualización incremental de clases:** solo cambia el rango entre el índice anterior y el nuevo.
4. **Clic en una palabra** para reposicionar (`set_position`) en cualquier modo. En pausa, la rueda del mouse desplaza libremente y, al reanudar, se toma la palabra que esté en la línea de lectura.
5. **Línea de lectura configurable** (entre el 30 % y el 50 % de la altura), con la retícula alineada.
6. **Medidor de nivel de micrófono** (en el setup y en el overlay), con un botón "Probar micrófono" y un selector de dispositivo de entrada.
7. **Atajos:**
   - un único keymap;
   - se ignoran si el foco está en `input`, `textarea`, `select` o `[contenteditable]`;
   - se quita el foco a los botones al entrar en lectura;
   - se agregan `+` y `-` (tamaño de letra en lectura) y `D` (overlay de depuración con latencias, confianza y transcript si `TP_DEBUG`);
   - se actualiza la ayuda en pantalla.
8. **Guiones y ajustes persistentes:** "Abrir de guiones/", "Guardar como" e importar un archivo local. El último guion, la posición y los ajustes se autoguardan en `data/settings.json` vía una API REST local; no se depende de localStorage.
9. **Fin del guion:** aparece un marcador "Fin" y se pausa el modo activo.
10. Opcionales, solo si sobra margen y lo confirmo: modo espejo (`M`) y cuenta regresiva 3-2-1 al iniciar.

### Fase 7 · Documentación y cierre (B1, B2, B4, B5)
1. Reescribe el README para que sea fiel al código: características reales, requisitos (Python 3.11), instalación, configuración (`TP_*`), atajos completos, modo nativo y solución de problemas (incluye dónde está el log).
2. Alinea `architecture/SOP_01..03` con la implementación nueva. Aplica D5 a `gemini.md`. Actualiza `findings.md`, `task_plan.md` y `progress.md` (con las métricas antes y después).
3. Corre la checklist de QA de la sección 10 y anota los resultados.
4. Entrégame un resumen final: qué cambió, métricas antes y después, decisiones pendientes y riesgos conocidos.

---

## 8. Protocolo WebSocket v2

**Cliente → servidor**
```jsonc
{"action": "hello"}                                         // al conectar; el servidor responde con "state"
{"action": "load_script", "text": "<texto completo>", "seq": 1}
{"action": "start", "idx": 120, "seq": 2}                   // fija la posición y empieza a escuchar (atómico)
{"action": "pause"}
{"action": "set_position", "idx": 135, "seq": 3}            // reemplaza a "reset" (idx 0) y a "step"
{"action": "set_input_device", "device": "<id o null>"}     // Fase 6
```

**Servidor → cliente**
```jsonc
{"type": "state", "model": "loading|ready|error", "listening": false,
 "script_id": "uuid", "total_tokens": 412, "current_idx": 0, "epoch": 3}
{"type": "script_loaded", "script_id": "uuid", "total_tokens": 412, "epoch": 1,
 "segments": [
   {"type": "word", "idx": 0, "text": "Hola,"},
   {"type": "directive", "text": "[Mostrar slide 2]", "at_idx": 3},
   {"type": "break", "kind": "paragraph"}
 ]}
{"type": "scroll", "current_idx": 42, "epoch": 3, "confidence": 0.91,
 "matched_word": "hola", "stt_ms": 110, "lag_ms": 480, "resync": false}
{"type": "status", "state": "loading|ready|listening|paused|desync|error",
 "code": "MIC_ERROR|NO_SCRIPT|MODEL_ERROR|BAD_REQUEST|...", "message": "texto para mostrar"}
{"type": "level", "rms_db": -32.5}                          // unas 10 veces por segundo mientras escucha
{"type": "transcript", "text": "...", "is_final": false}    // SOLO con TP_DEBUG=1
```

**Reglas:**
- `seq` es un contador monotónico del cliente. El servidor lo adopta como época y lo incluye en cada `scroll`. El cliente ignora todo `scroll` con una época menor a la última que envió. El worker anota la época al tomar el audio y descarta su resultado si la época cambió mientras transcribía.
- Una conexión nueva con un `Origin` válido reemplaza a la anterior, que se cierra con el código `4000` ("reemplazada"). El cliente que recibe `4000` no reintenta y muestra "Abierto en otra ventana". Un `Origin` inválido se rechaza con `1008`.
- Un envío único por una cola de salida. Si se acumulan `scroll`, se envía solo el último.
- Un payload inválido responde `status: error, code: BAD_REQUEST` y no cierra la conexión.
- Documenta este protocolo en `architecture/SOP_03_websocket_protocol.md` y en `CLAUDE.md`, y elimina de los documentos lo que no se implemente (por ejemplo, el heartbeat, si no hace falta).

---

## 9. Pruebas

- **`tests/test_parser.py`:** casos borde de la Fase 1.1; índices 1:1 con los segmentos dibujados; unidades de `RTX-5070`, `uno/dos`, números y signos sueltos; `at_idx` de directivas consecutivas.
- **`tests/test_matcher.py`:** lectura normal; palabras partidas entre ventanas; palabras repetidas cerca; solo stopwords (no avanza); alucinaciones de la lista negra (no avanza); salto de párrafo (resincroniza); relectura de una frase (no retrocede salvo con coincidencia fuerte); fin del guion; tokens sin unidades.
- **`tests/test_protocol.py`** (con `TestClient` de FastAPI y un STT falso inyectable, por ejemplo con `TP_FAKE_STT=1`): `hello` → `state`; `load_script` → `script_loaded`; `start` con índice; épocas (se descartan resultados viejos); una segunda conexión reemplaza a la primera; `Origin` inválido se rechaza; un payload inválido no cierra la conexión; sin `TP_DEBUG` no hay `transcript`.
- **`tests/test_api.py`:** API de guiones y ajustes, incluido el intento de path traversal.
- **Replay:** las grabaciones de la Fase 0 con las métricas y objetivos de la Fase 2. No se versionan.
- Todos los tests deben correr sin GPU, sin micrófono y sin red (STT falso y fixtures de texto).

---

## 10. Checklist de QA manual

1. `start.bat`: el navegador abre y muestra "Cargando modelo…", y pasa a listo sin recargar a mano.
2. `start_stealth.bat`: la ventana aparece en menos de 2 s con el estado de carga. Sin el modelo descargado, muestra un mensaje claro en vez de fallar a los 60 s.
3. Guion con `—`, `...`, `RTX-5070`, números, directivas consecutivas y párrafos: se dibuja y resalta correctamente.
4. Manos libres con `guiones/ejemplo.txt` a ritmo normal: el texto sigue la voz sin saltos. Al saltarse un párrafo, resincroniza en unos 3 s. Al releer una frase, no se desordena. Con 15 s de silencio, una tos o ruido, no avanza.
5. Auto-scroll, pausa y luego Manos libres: continúa en la misma palabra.
6. Flechas o clic en una palabra durante Manos libres: no vuelve después a la posición anterior.
7. Recargar la página o cerrar y reabrir durante Manos libres: se restauran el guion y la posición.
8. Desconectar el micrófono durante la lectura: aparece un error visible en modo lectura y el modo pasa a pausa.
9. Oculto en capturas: la ventana no aparece en la captura de ventana ni en la de pantalla de OBS, en Meet, Zoom o Teams al compartir pantalla, ni con Win+Shift+S. L lo alterna. T alterna "Siempre adelante" y la ventana se sigue moviendo y redimensionando con fluidez.
10. Lanzar dos veces la app nativa: la segunda trae al frente la primera.
11. Cerrar la ventana: `pythonw` termina y el puerto 8765 queda libre.
12. Sin internet: todo funciona y DevTools no muestra ninguna petición externa.
13. Una página de otro origen (por ejemplo, servida con `python -m http.server 9000`) que intenta abrir el WebSocket es rechazada.
14. Con el foco en un slider, los atajos no disparan acciones accidentales.
15. Al reiniciar la app se conservan el último guion, la posición y los ajustes.

---

## 11. Definición de terminado

- Todos los hallazgos de la sección 4 están corregidos o justificados por escrito (en `progress.md`) si se descartaron tras verificarlos.
- `pytest` pasa completo sin GPU, sin micrófono y sin red.
- Las métricas de replay cumplen los objetivos de la Fase 2 (o la constitución se actualizó con las cifras reales y su justificación).
- La checklist de QA de la sección 10 está completa, con sus resultados anotados.
- README, `CLAUDE.md`, los SOP y `progress.md` reflejan el código real.
- La rama `fix/pulido-integral` tiene commits por fase, listos para revisar y fusionar.

---

## 12. Observaciones adicionales del usuario (opcional)

> Si esta sección tiene contenido, trátalo como prioridad máxima e intégralo en la fase que corresponda.

-
