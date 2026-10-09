# Privacidad: ejecución 100% local (innegociable)

Aplica a todo el repositorio y prevalece sobre cualquier conveniencia. Si un cambio la contradice, no se hace: se consulta al usuario.

## Red

- Cero peticiones de red en tiempo de ejecución: nada de fuentes web (Google Fonts), CDN, telemetría, analytics, comprobación de actualizaciones ni servicios externos.
- `frontend/` (HTML, CSS y JS) no contiene URLs externas: ni `<link>` o `<script>` remotos, ni `@import` remoto, ni `fetch` o `WebSocket` a otro host que no sea el propio origen. Se usan rutas `/static/...` o relativas.
- La red solo se usa al instalar: `pip install` y la descarga del modelo durante `scripts\install.bat` (`tools/download_model.py`). Ningún otro módulo ni script descarga nada.
- El servidor hace bind solo en `127.0.0.1`. Nunca `0.0.0.0` ni otra interfaz.
- En tiempo de ejecución el modelo se carga con `local_files_only=True` y `HF_HUB_OFFLINE=1` (definido antes de importar `faster_whisper` o `huggingface_hub`). Si el modelo falta, se informa un error claro (`MODEL_ERROR`) que indica correr `install.bat`; nunca se descarga en caliente.
- No se agregan dependencias que abran conexiones por su cuenta. Si una librería trae telemetría, se desactiva explícitamente o no se usa.

## WebSocket

- `/ws` valida el `Origin`: solo `http://127.0.0.1:<puerto>` y `http://localhost:<puerto>` (y el origen que envíe WebView2 en la app nativa, verificado y documentado). El resto se rechaza con el código 1008 y se registra en el log.
- Los mensajes `transcript` solo se envían con `TP_DEBUG=1`. Sin esa variable, ningún texto transcrito sale del backend.
- Ningún endpoint REST devuelve audio ni transcripciones.

## Disco

- No se escribe audio ni transcripciones a disco: ni en `logs/`, ni en `data/`, ni en temporales. Los logs registran eventos, tiempos, índices y códigos de error, nunca el texto dicho.
- Única excepción: las grabaciones que el usuario pide explícitamente con `tools/record_sample.py`, en `tests/fixtures/audio/` (ignorada por git), y las salidas de replay derivadas de ellas en esa misma carpeta.
- `data/` guarda solo los guiones y ajustes que el usuario decide guardar.
- Nunca se versionan `tests/fixtures/audio/`, `data/`, `logs/`, `models/` ni `.venv*/`.

## Cómo comprobarlo

- `Grep` de `https?://` en `frontend/`: solo se admiten `127.0.0.1` y `localhost` (y el texto de licencias de fuentes, por ejemplo `frontend/fonts/OFL.txt`).
- `Grep` de `requests`, `httpx`, `urllib`, `urlopen`, `snapshot_download` y `hf_hub_download` en `backend/`, `tools/` y `launch.py`: solo se admiten en `tools/download_model.py` y en los tests (sin red real).
- Con el backend corriendo: `Get-NetTCPConnection -OwningProcess <PID>` no muestra conexiones a direcciones remotas.
- `tests/test_protocol.py` cubre el rechazo de `Origin` ajeno y la ausencia de `transcript` sin `TP_DEBUG`. Esos tests no se debilitan ni se borran.
