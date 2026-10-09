# Diseño v2 · Contrato técnico del pulido integral

Este documento es el contrato que siguen todas las fases del plan `PROMPT_MAESTRO_CORRECCIONES.md`.
Si el código y este documento discrepan, se corrige uno de los dos en el mismo commit.
Al cerrar la Fase 7, su contenido se reparte en `CLAUDE.md` y en `architecture/SOP_01..03`.

## 1. Principios

1. Ejecución 100% local: ninguna petición de red en tiempo de ejecución. Bind solo en `127.0.0.1`.
2. Una sola fuente de verdad para el guion: el backend parsea; el frontend dibuja.
3. Posiciones absolutas y épocas: el cliente nunca envía deltas.
4. Ningún error silencioso: todo fallo llega al usuario como `status` con `code` y queda en el log.
5. Atomicidad: cada módulo de `tools/` hace una sola cosa; la composición vive en `backend/`.
6. Todo se prueba sin GPU, sin micrófono y sin red (STT y audio falsos inyectables).

## 2. Mapa de módulos

| Módulo | Responsabilidad única |
|---|---|
| `tools/text_norm.py` | Normalizar texto a unidades de coincidencia (NFD, sin tildes, minúsculas, números a palabras). |
| `tools/script_parser.py` | Convertir el texto del guion en segmentos (`word`, `directive`, `break`) y tokens con unidades. |
| `tools/script_matcher.py` | Alinear palabras transcritas con las unidades del guion y decidir el nuevo índice. |
| `tools/transcript_filter.py` | Descartar segmentos y transcripciones que son alucinaciones o ruido (Fase 2). |
| `tools/audio_capture.py` | Capturar el micrófono a un buffer circular de 16 kHz mono, con nivel en dBFS. |
| `tools/audio_file_source.py` | Fuente de audio que reproduce un WAV en tiempo real con la misma interfaz que la captura (pruebas y demos). |
| `tools/vad_gate.py` | Decidir si hubo voz en el último tramo de una ventana (Silero VAD de faster-whisper) (Fase 2). |
| `tools/stt_engine.py` | Cargar Whisper y transcribir una ventana de audio. |
| `tools/cuda_dlls.py` | Registrar las DLL de CUDA instaladas por pip. |
| `tools/replay.py` | Simular el pipeline sobre un WAV y medir la sincronización (Fase 2). |
| `tools/record_sample.py` | Grabar muestras del micrófono a WAV. |
| `tools/download_model.py` | Descargar el modelo durante la instalación (Fase 5). |
| `backend/config.py` | Única fuente de configuración (`TP_*`, `config.json`, valores por defecto). |
| `backend/protocol.py` | Validar mensajes del cliente y construir mensajes del servidor. |
| `backend/fakes.py` | STT y audio falsos para pruebas (`TP_FAKE_STT=1`). |
| `backend/voice.py` | Bucle de voz: compone captura, VAD, STT, filtro y matcher. |
| `backend/server.py` | App FastAPI: sesión, conexión única, cola de salida, rutas. |

## 3. Normalización y unidades (`tools/text_norm.py`)

```python
STOPWORDS_ES: frozenset[str]            # artículos, preposiciones, conjunciones, pronombres átonos
def fold(s: str) -> str                 # NFKD, quita marcas Mn, minúsculas (no toca dígitos ni signos)
def to_units(s: str) -> list[str]       # unidades de coincidencia de un texto cualquiera
def is_stopword(unit: str) -> bool
```

Reglas de `to_units`:

1. `fold(s)`; la ñ se convierte en n (efecto natural de la descomposición, aceptado). Se usa NFKD y no NFD porque además pliega formas de compatibilidad: `º` y `ª` pasan a `o` y `a`, `²` a `2`, las ligaduras (`ﬁ`) a letras sueltas y los dígitos de ancho completo a ASCII.
2. Se extraen las corridas alfanuméricas (`[^\W_]+`) y cada una se separa en la frontera entre letras y dígitos (`5070ti` da `5070`, `ti`).
3. Cada corrida de dígitos se convierte con `num2words(n, lang="es")` y se vuelve a normalizar y dividir (`5070` da `cinco`, `mil`, `setenta`). Si la corrida tiene más de 12 dígitos se deja como está.
4. Todo lo demás (signos, guiones, barras, emojis) separa unidades y no produce ninguna.

Ejemplos: `RTX-5070` da `["rtx", "cinco", "mil", "setenta"]`; `uno/dos` da `["uno", "dos"]`; `e-mail` da `["e", "mail"]`; `—`, `...`, `¿`, `?` y `🚀` dan `[]`; `¿Funciona?` da `["funciona"]`.

La transcripción de Whisper se normaliza con la misma función, así que dígitos y palabras coinciden en ambos sentidos.

## 4. Parser del guion (`tools/script_parser.py`)

```python
@dataclass
class Token:
    idx: int
    text: str          # tal como se muestra (con puntuación)
    units: list[str]   # vacía = token que el matcher salta

@dataclass
class ParsedScript:
    script_id: str             # uuid4 en hex
    text: str                  # texto original normalizado a LF
    segments: list[dict]       # ver abajo, listo para JSON
    tokens: list[Token]        # uno por segmento word, en orden, idx == posición
    total_tokens: int

def parse_script(text: str) -> ParsedScript
MAX_SCRIPT_CHARS = 2_000_000
```

Segmentos (el mismo formato viaja en `script_loaded`):

```jsonc
{"type": "word", "idx": 0, "text": "Hola,"}
{"type": "word", "idx": 1, "text": "—", "skip": true}      // sin unidades
{"type": "directive", "text": "[Mostrar slide 2]", "at_idx": 3}
{"type": "break", "kind": "paragraph"}                      // o "line"
```

Reglas:

1. CRLF y CR pasan a LF; los tabs cuentan como espacio.
2. Directivas: empiezan en `[` y terminan en el `]` que la cierra contando anidamiento (`[a [b] c]` es una sola directiva). Pueden ocupar varias líneas; su `text` conserva el contenido con los saltos internos convertidos en espacios y los espacios repetidos colapsados. Un `[` sin cierre es texto normal (se tokeniza como palabras). Un `]` suelto también es texto.
3. `at_idx` de una directiva es el índice de la primera palabra que la sigue, o `total_tokens` si no hay ninguna.
4. Saltos: una o más líneas en blanco entre contenido producen un `break` de tipo `paragraph`; un salto simple produce `line`. Nunca hay `break` al principio, al final ni dos seguidos (gana `paragraph`). Una directiva ya se dibuja como bloque, así que los saltos que la rodean se conservan solo si son `paragraph`.
5. Palabras: secuencias separadas por espacio en blanco. Todas reciben índice, incluidas las que no tienen unidades (con `"skip": true`).
6. Texto vacío o solo espacios produce cero tokens y cero segmentos. Textos mayores que `MAX_SCRIPT_CHARS` lanzan `ValueError`.

Clasificación de directivas en el frontend: leída si `current_idx > at_idx`, actual si `current_idx == at_idx`, próxima en el resto. Directivas consecutivas comparten `at_idx` y se activan juntas.

## 5. Matcher (`tools/script_matcher.py`)

Interfaz común (la Fase 1 adapta el algoritmo voraz actual a unidades; la Fase 2 la reemplaza por el alineamiento):

```python
@dataclass
class MatchResult:
    new_idx: int
    advanced: bool
    confidence: float          # 0..1
    matched_word: str | None   # texto del último token alineado
    resync: bool = False       # salto por resincronización (Fase 2)
    desync: bool = False       # el matcher está desincronizado (Fase 2)

class ScriptMatcher:
    def __init__(self, tokens: list[Token] | None = None, **params) -> None
    def load(self, tokens: list[Token]) -> None        # posición 0
    def set_position(self, idx: int) -> int            # recorta a [0, total], devuelve el valor aplicado
    current_idx: int                                   # propiedad de solo lectura
    def update(self, words: list[str]) -> MatchResult  # words ya normalizadas con to_units
```

Matcher v2 (Fase 2):

1. Ventana local: tokens `[current_idx - 10, current_idx + 25]` aplanados a unidades, con un mapa unidad a índice de token. Los tokens sin unidades no aportan unidades.
2. Alineamiento local por programación dinámica (Smith-Waterman) entre las palabras de la transcripción y esas unidades. Puntajes iniciales: palabra de contenido exacta +2,0; stopword exacta +0,5; coincidencia difusa (Levenshtein 1 con ambas palabras de más de 3 letras) +1,2; sustitución -1,0; hueco -0,6.
3. Se acepta un avance si el puntaje es mayor o igual que `min_score` (3,0 inicial), hay al menos una palabra de contenido alineada y la última palabra alineada de la transcripción está entre las últimas 3.
4. `new_idx = último token alineado + 1`, luego se saltan los tokens sin unidades; nunca baja (monótono) y avanza como máximo `max_advance` tokens por ciclo (8 inicial).
5. Un alineamiento aceptado que termina antes de `current_idx` (relectura) no avanza, pero cuenta como "en sincronía".
6. Desincronización: si `stall_cycles` (6) llamadas consecutivas a `update` con palabras no logran ningún alineamiento aceptable, se marca `desync` y se busca en `[current_idx - 40, current_idx + 200]` un alineamiento fuerte (4 o más palabras de contenido consecutivas). Si existe, se salta ahí (adelante o atrás) con `resync=True`. Es la única vía para retroceder. Las transcripciones vacías o filtradas no cuentan como ciclo.
7. Todos los parámetros se pasan por `**params` para que el replay pueda barrerlos.

## 6. Protocolo WebSocket v2

Endpoint: `ws://127.0.0.1:<puerto>/ws`. Mensajes JSON de texto, un objeto por mensaje.

### Cliente a servidor

```jsonc
{"action": "hello", "client": "flow-local-web/2"}        // al abrir; respuesta: state (+ script_loaded si hay guion)
{"action": "load_script", "text": "<texto>", "seq": 1}   // respuesta: script_loaded + state
{"action": "start", "idx": 120, "seq": 2}                // fija posición y escucha (atómico)
{"action": "pause"}
{"action": "set_position", "idx": 135, "seq": 3}         // reemplaza a reset (idx 0) y a step
{"action": "set_input_device", "device": 3}              // Fase 6 (índice, o null para el predeterminado)
{"action": "mic_test", "on": true}                       // Fase 6 (solo nivel, sin STT)
```

Validación (`backend/protocol.py`): `action` debe ser un string conocido; `seq` entero (no bool) mayor o igual que 0; `idx` entero (no bool) en `[0, total_tokens]`; `text` string no vacío tras `strip()` y de como máximo `MAX_SCRIPT_CHARS`. Mensajes que no son JSON, que no son objetos o que superan 4 MB también son inválidos. Los campos desconocidos se ignoran. Un mensaje inválido produce `status` con `state: "error"` y `code: "BAD_REQUEST"`, y la conexión sigue abierta.

### Servidor a cliente

```jsonc
{"type": "state", "protocol": 2, "model": "loading|ready|error", "model_name": "medium",
 "device": "cuda", "listening": false, "script_id": "hex|null", "total_tokens": 412,
 "current_idx": 0, "epoch": 3, "debug": false}
{"type": "script_loaded", "script_id": "hex", "total_tokens": 412, "epoch": 1,
 "text": "<texto original>", "segments": [ ... ]}
{"type": "scroll", "current_idx": 42, "epoch": 3, "confidence": 0.91, "matched_word": "hola",
 "stt_ms": 110, "lag_ms": 480, "resync": false}
{"type": "status", "state": "loading|ready|listening|paused|desync|error|warning",
 "code": "MIC_ERROR|...|null", "message": "texto para mostrar"}
{"type": "level", "rms_db": -32.5}                       // unas 10 veces por segundo mientras escucha
{"type": "transcript", "text": "...", "is_final": false} // solo con TP_DEBUG=1
```

Códigos: `BAD_REQUEST`, `NO_SCRIPT`, `MODEL_NOT_READY`, `MODEL_ERROR`, `MODEL_MISSING`, `MIC_ERROR`, `MIC_LOST`, `CUDA_FALLBACK` (warning), `INTERNAL`.

### Semántica

- **Épocas.** `seq` es un contador monotónico del cliente. Al procesar `load_script`, `start` o `set_position`, el servidor adopta `seq` como `epoch` y lo incluye en cada `scroll`. El cliente ignora todo `scroll` con `epoch` menor que el último `seq` que envió. El bucle de voz anota la época al tomar el audio y descarta el resultado si cambió mientras transcribía. Al recibir `state`, el cliente ajusta su contador a `max(contador, epoch)` para seguir siendo monotónico tras recargar la página.
- **`set_position`.** Recorta y aplica la posición, adopta la época, marca como descartado todo el audio capturado hasta ese instante (el bucle solo usa muestras posteriores) y responde con un `scroll` de eco (`confidence: 1.0`, `stt_ms: 0`, `lag_ms: 0`).
- **`start`.** Requiere guion (`NO_SCRIPT`) y modelo listo (`MODEL_NOT_READY`). Aplica `idx` y `seq` como en `set_position`; si ya escuchaba solo reposiciona. Si el micrófono falla responde `MIC_ERROR` y queda en pausa. Si todo va bien responde `status: listening` y el eco de `scroll`.
- **`load_script`.** Si estaba escuchando, primero pausa (`status: paused`). Parsea, deja la posición en 0, adopta la época y responde `script_loaded` y `state`.
- **Conexión única con reemplazo.** Una conexión nueva (con `Origin` válido, Fase 4) reemplaza a la anterior, que se cierra con el código `4000` ("reemplazada"). El cliente que recibe `4000` no reintenta y muestra "Abierto en otra ventana" con un botón para retomar el control. Al perder o reemplazar el cliente activo, el servidor deja de escuchar (micrófono apagado sin cliente).
- **Estado de sesión persistente.** Guion, posición, época y estado del modelo viven en el servidor y sobreviven a las reconexiones. Tras `hello`, si hay guion, el servidor envía también `script_loaded`, así una página recargada recupera guion y posición.
- **Cola de salida única.** Cada conexión tiene una sola tarea emisora. Si hay varios `scroll` (o varios `level`) pendientes, solo se envía el último de cada tipo; el resto mantiene su orden.
- **Origin (Fase 4).** Solo se aceptan `http://127.0.0.1:<puerto>` y `http://localhost:<puerto>`; el resto se cierra con `1008` y queda en el log.

## 7. Frontend (`frontend/app.js`)

Máquina de estados explícita:

- Conexión: `connecting`, `open`, `closed` (reconexión con backoff 0,5 s a 8 s), `replaced` (código 4000, sin reintento).
- Modelo: `unknown`, `loading`, `ready`, `error` (desde `state` y `status`).
- Modo: `idle`, `voice_pending` (se envió `start`), `voice`, `auto`.

Reglas:

1. La UI pasa a "VOZ" solo al recibir `status: listening`. Ante `status: error` vuelve a `idle` y muestra el aviso. Si en 6 s no llega respuesta a `start`, vuelve a `idle` con aviso.
2. Al cerrarse la conexión: si el modo era voz, pasa a `idle`; el Auto-scroll sigue funcionando porque es local. Se muestra "Reconectando…".
3. Al abrir la conexión envía `hello`. Con `state` + `script_loaded` recupera guion y posición. Si el cliente tiene un guion local y el servidor no tiene ninguno (o tiene otro `script_id`), reenvía `load_script` y luego `set_position` con su posición.
4. `send()` sin conexión avisa al usuario; las acciones de posición y guion se resincronizan solas al reconectar (paso 3), así que no hace falta encolarlas.
5. El guion se dibuja desde `segments`: párrafos como bloques (`<p>`), saltos de línea como `<br>`, directivas como bloques. Un arreglo `wordEls[idx]` da acceso O(1); al cambiar de índice solo se actualizan las clases del rango entre el índice anterior y el nuevo, y las directivas por su `at_idx`.
6. Auto-scroll solo requiere guion cargado, no el modelo. Pasar de Auto (o de pausa) a Voz envía `start` con el índice actual. Flechas y reinicio envían `set_position`.
7. Avisos visibles en ambos modos: una barra de notificación fija arriba (toast) y en el overlay un indicador de conexión y de estado del modelo.
8. Un único keymap. Se ignora si el foco está en `input`, `textarea`, `select` o `[contenteditable]`, o si hay modificadores (salvo Shift en flechas). Al entrar en modo lectura se quita el foco de los botones.

## 8. Audio y bucle de voz

`AudioCapture` (`tools/audio_capture.py`) y `AudioFileSource` (`tools/audio_file_source.py`) comparten esta interfaz:

```python
sample_rate: int = 16000
def start(self) -> None                  # lanza la excepción si el dispositivo falla
def stop(self) -> None
total_samples: int                       # contador global de muestras escritas
level_db: float                          # nivel RMS en dBFS del último bloque de ~100 ms
last_callback_ts: float                  # time.monotonic() del último bloque recibido
def read_window(self, seconds: float, since_sample: int = 0) -> tuple[np.ndarray, int]
    # devuelve (muestras de los últimos `seconds` posteriores a since_sample, total_samples al leer)
```

- Buffer circular de `window_s * 2` segundos; nunca descarta audio por RMS.
- Abre a 16 kHz; si el dispositivo lo rechaza, abre a su tasa nativa y remuestrea con `av.AudioResampler`.
- Dispositivo elegible con `TP_INPUT_DEVICE` (índice) y listable vía API (Fase 6).
- Vigilancia: si no llegan bloques durante 1,5 s mientras escucha, el bucle emite `MIC_LOST` y pausa.

Bucle de voz (`backend/voice.py`), Fase 2:

1. Cada `TP_STEP_MS` (350 por defecto) toma la ventana de los últimos `TP_WINDOW_S` (3,0) segundos posteriores al último reposicionamiento.
2. Si la ventana dura menos de 1 s, o el VAD no ve voz en su último segundo, no transcribe.
3. Transcribe con `beam_size=1`, `temperature=0`, `condition_on_previous_text=False`, `without_timestamps=True`, `vad_filter=False`, idioma fijo y opcionalmente `initial_prompt` con las 20 a 30 palabras del guion anteriores a `current_idx` (`TP_PROMPT_CONTEXT=1`).
4. La inferencia se espera dentro del bucle, así que nunca hay dos a la vez ni se acumula retraso: el ciclo siguiente toma siempre la ventana más reciente.
5. Filtra alucinaciones, normaliza con `to_units`, comprueba la época y llama al matcher; envía `scroll` si avanzó, `status: desync` y `status: listening` al entrar y salir de la desincronización, y `transcript` solo con `TP_DEBUG`.
6. `lag_ms` mide desde el instante de captura de la última muestra de la ventana hasta el envío del `scroll`; `stt_ms` mide solo la inferencia.

En la Fase 1 el bucle conserva la semántica anterior (bloques nuevos de 500 ms sin solapamiento y matcher voraz adaptado a unidades), pero ya usa esta interfaz de captura y las épocas.

## 9. STT (`tools/stt_engine.py`)

```python
@dataclass
class SegmentInfo: text: str; no_speech_prob: float; avg_logprob: float; compression_ratio: float
@dataclass
class Transcription: text: str; segments: list[SegmentInfo]; stt_ms: int

class STTEngine:
    def __init__(self, model_size, device, compute_type, language): ...
    def load(self) -> None            # local_files_only=True; registra DLL de CUDA; warmup con 1 s de silencio
    def transcribe(self, samples: np.ndarray, initial_prompt: str | None = None) -> Transcription
    device_in_use: str                # "cuda" o "cpu" tras un fallback
```

- Si falta el modelo en el disco: excepción con código `MODEL_MISSING` y un mensaje que indica cómo descargarlo.
- Si CUDA falla al cargar o en el warmup: reintenta en CPU con `int8` y avisa con `CUDA_FALLBACK`.
- `FakeSTTEngine` (`backend/fakes.py`) implementa la misma interfaz y devuelve transcripciones programadas.

## 10. Configuración (`backend/config.py`)

Precedencia: variable de entorno, luego `config.json` en la raíz (opcional, ignorado por git), luego el valor por defecto. Un valor inválido se registra como advertencia y se usa el valor por defecto; nunca rompe el arranque.

| Variable | Defecto | Uso |
|---|---|---|
| `TP_PORT` | `8765` | Puerto local (el host es siempre `127.0.0.1`). |
| `TP_MODEL_SIZE` | `medium` | Modelo de faster-whisper. |
| `TP_DEVICE` | `cuda` | `cuda` o `cpu`. |
| `TP_COMPUTE` | `float16` | Tipo de cómputo de CTranslate2. |
| `TP_LANGUAGE` | `es` | Idioma fijo. |
| `TP_INPUT_DEVICE` | vacío | Índice del dispositivo de entrada (vacío: predeterminado). |
| `TP_WINDOW_S` | `3.0` | Ventana de audio por inferencia. |
| `TP_STEP_MS` | `350` | Periodo del bucle de voz. |
| `TP_PROMPT_CONTEXT` | `0` | Usar el guion previo como `initial_prompt`. |
| `TP_VAD_THRESHOLD` | `0.5` | Umbral de Silero VAD. |
| `TP_DEBUG` | `0` | Habilita `transcript` y el overlay de depuración. |
| `TP_FAKE_STT` | `0` | STT y audio falsos (pruebas). |
| `TP_AUDIO_FILE` | vacío | Usar un WAV como micrófono en tiempo real (pruebas y demos). |
| `TP_LOG_LEVEL` | `INFO` | Nivel del log. |

## 11. Pruebas

- `tests/test_text_norm.py`, `tests/test_parser.py`, `tests/test_matcher.py`, `tests/test_protocol.py`, `tests/test_api.py`.
- Las pruebas de protocolo usan `TestClient` de FastAPI con `TP_FAKE_STT=1` y una fábrica de app (`create_app(settings)`), sin dormir más de lo imprescindible.
- Nada de GPU, micrófono ni red en `pytest`.
