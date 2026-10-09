---
description: "Recorre la checklist de QA manual (sección 10 del plan maestro), automatiza lo posible y registra los resultados en progress.md"
argument-hint: "[números de ítem, por ejemplo: 3 12 13]"
---

# /qa

Recorre la checklist de QA de la sección 10 de `PROMPT_MAESTRO_CORRECCIONES.md`, ítem por ítem. Ítems pedidos: `$ARGUMENTS` (vacío: los 15).

## Cómo trabajar

1. Antes de empezar: anota rama y commit (`git rev-parse --abbrev-ref HEAD`, `git rev-parse --short HEAD`), confirma que existe `.venv312\Scripts\python.exe` y corre una vez `.venv312\Scripts\python.exe -m pytest -q` (resultado global).
2. Para cada ítem, en orden:
   - **Automático:** ejecuta las comprobaciones indicadas y guarda la evidencia (salida resumida, tiempos, cantidades).
   - **Manual:** muestra al usuario los pasos exactos, pregúntale el resultado (OK o FALLA, con una nota) y espera su respuesta. Si prefiere dejarlo para después, márcalo PENDIENTE.
   - Un ítem con parte manual nunca queda OK solo por las comprobaciones automáticas.
3. Si algo que se quiere comprobar todavía no existe en el código (por ejemplo `backend/config.py`, `data/settings.json` o un test), anótalo como "no implementado" en vez de inventar la verificación.
4. Los ítems 1, 3, 12 y 13 necesitan el backend en modo navegador: arráncalo como en `/run` (comprobando antes el puerto 8765) y detenlo al terminar si lo arrancaste tú. Antes de los ítems de la app nativa (2, 9, 10 y 11), detenlo y confirma que el puerto quedó libre.
5. No escribas audio ni transcripciones a disco y no copies texto transcrito en el informe.

## Utilidades

Puerto y procesos (PowerShell):

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress, OwningProcess
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" | Where-Object CommandLine -match 'launch\.py|uvicorn' | Select-Object ProcessId, CommandLine
```

Sonda WebSocket: guárdala en el scratchpad de la sesión (nunca en el repo) como `qa_ws_probe.py` y córrela con `.venv312\Scripts\python.exe <ruta>\qa_ws_probe.py <origen|modelo|guion>`. Cada conexión válida reemplaza a la de la UI abierta (código 4000): úsala con la UI cerrada o recárgala después. Si todo sale RECHAZADA con `ConnectionRefusedError`, el backend no está corriendo.

```python
import asyncio, json, sys, time
from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:8765/ws"
OK_ORIGIN = "http://127.0.0.1:8765"
TEXTO = ("Hola \u2014 mundo. Esto es la RTX-5070 ... y el sistema ¿funciona ? sí.\n\n"
         "[Cambiar a slide 2] [Mostrar grafico] Son 15 pasos y 3,5 segundos.\n\nFin.")

async def hello(origin):
    async with connect(URL, origin=origin, open_timeout=5) as ws:
        await ws.send(json.dumps({"action": "hello"}))
        return json.loads(await asyncio.wait_for(ws.recv(), 5))

async def origen():
    for o in [OK_ORIGIN, "http://localhost:8765", "http://127.0.0.1:9000", "https://ejemplo.invalid"]:
        try:
            print(o, "-> ACEPTADA", (await hello(o)).get("type"))
        except Exception as e:
            print(o, "-> RECHAZADA", type(e).__name__, e)

async def modelo(limite=180):
    t0 = time.time()
    while time.time() - t0 < limite:
        try:
            m = await hello(OK_ORIGIN)
            print(f"{time.time() - t0:5.1f} s model={m.get('model')}")
            if m.get("model") in ("ready", "error"):
                return
        except Exception as e:
            print(f"{time.time() - t0:5.1f} s sin conexión: {type(e).__name__}")
        await asyncio.sleep(1)

async def guion():
    async with connect(URL, origin=OK_ORIGIN, open_timeout=5) as ws:
        await ws.send(json.dumps({"action": "load_script", "text": TEXTO, "seq": 1}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m.get("type") in ("script_loaded", "status"):
                print(json.dumps(m, ensure_ascii=False, indent=1))
                if m.get("type") == "script_loaded" or m.get("state") == "error":
                    return

asyncio.run({"origen": origen, "modelo": modelo, "guion": guion}[sys.argv[1]]())
```

## Checklist (sección 10)

### 1. Arranque en modo navegador
> `start.bat`: el navegador abre y muestra "Cargando modelo…", y pasa a listo sin recargar a mano.

- Automático: revisa `scripts\start.bat` (qué venv usa y que no sobrescriba variables `TP_*`). Arranca el backend como en `/run` midiendo cuánto tarda el puerto en responder (con el arranque de la Fase 3 debe ser casi inmediato, menos de 3 s) y corre la sonda `modelo` para medir el paso de `loading` a `ready`.
- Manual: ejecuta `scripts\start.bat`. Comprueba que el navegador abre, muestra "Cargando modelo…" y pasa a listo solo, sin F5. Anota el tiempo de carga.

### 2. Arranque de la app nativa
> `start_stealth.bat`: la ventana aparece en menos de 2 s con el estado de carga. Sin el modelo descargado, muestra un mensaje claro en vez de fallar a los 60 s.

- Automático: con el puerto libre, mide el tiempo hasta que la ventana es visible:
  ```powershell
  $t0 = Get-Date; Start-Process -FilePath (Resolve-Path scripts\start_stealth.bat) -WindowStyle Hidden
  do { Start-Sleep -Milliseconds 100
    $w = Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0
  } until ($w -or ((Get-Date) - $t0).TotalSeconds -gt 15)
  "ventana visible en {0:N1} s" -f ((Get-Date) - $t0).TotalSeconds
  ```
  Busca en `backend/` y `tools/` el manejo de modelo ausente (`local_files_only`, `MODEL_ERROR`).
- Manual: confirma que la ventana muestra el estado de carga desde el primer momento. Para el caso sin modelo: cierra la app; en una consola nueva apunta la caché del modelo a una carpeta vacía (la variable que use `backend/config.py`, por ejemplo `$env:HF_HOME = "$env:TEMP\tp_qa_sin_modelo"`), lanza `scripts\start_stealth.bat` desde esa misma consola y verifica que aparece un mensaje claro (qué falta y cómo resolverlo). Cierra esa consola al terminar.

### 3. Guion con casos borde
> Guion con guion largo (U+2014), `...`, `RTX-5070`, números, directivas consecutivas y párrafos: se dibuja y resalta correctamente.

(El carácter se nombra por su código para cumplir la regla de documentación.)

- Automático: `.venv312\Scripts\python.exe -m pytest -q tests/test_parser.py`. Con el backend corriendo, sonda `guion`: los `idx` de las palabras son consecutivos desde 0 y sin huecos; el guion largo y `...` tienen índice propio; `RTX-5070` es un solo token; las dos directivas tienen el mismo `at_idx` (el de "Son"); hay `break` de tipo `paragraph` entre párrafos.
- Manual: pega el mismo texto (el guion largo se escribe con Alt+0151), pulsa "Cargar guion" y comprueba: párrafos como bloques separados, directivas visibles y separadas del texto; con Auto-scroll el resaltado cae siempre en la palabra correcta y las directivas pasan de próxima a actual y a leída en orden.

### 4. Manos libres con voz real
> Manos libres con `guiones/ejemplo.txt` a ritmo normal: el texto sigue la voz sin saltos. Al saltarse un párrafo, resincroniza en unos 3 s. Al releer una frase, no se desordena. Con 15 s de silencio, una tos o ruido, no avanza.

- Automático: corre `/replay` sobre `tests\fixtures\audio\` y compara con los objetivos de la Fase 2.
- Manual: con `guiones\ejemplo.txt` en Manos libres: (a) lee a ritmo normal; (b) sáltate un párrafo y cronometra la resincronización; (c) relee una frase; (d) quédate 15 s en silencio, tose y haz ruido (teclado, silla). Anota lo observado en cada caso.

### 5. Auto-scroll y luego Manos libres
> Auto-scroll, pausa y luego Manos libres: continúa en la misma palabra.

- Automático: busca en `tests/` los tests de `start` con `idx` y córrelos.
- Manual: carga `guiones\ejemplo.txt`, activa Auto-scroll, pausa en una palabra reconocible (anótala), activa Manos libres y lee desde esa palabra: el resaltado sigue ahí o más adelante, nunca vuelve atrás.

### 6. Reposicionar durante Manos libres
> Flechas o clic en una palabra durante Manos libres: no vuelve después a la posición anterior.

- Automático: busca en `tests/` los tests de épocas (`seq`, `epoch`) y córrelos.
- Manual: en Manos libres pulsa → varias veces (o haz clic en una palabra más adelante) y sigue leyendo desde ahí: en los 5 s siguientes no vuelve a la posición vieja. Repite con ← y con clic en una palabra anterior.

### 7. Recargar o reabrir
> Recargar la página o cerrar y reabrir durante Manos libres: se restauran el guion y la posición.

- Automático: busca y corre los tests de `hello` → `state` (`script_id`, `current_idx`).
- Manual: a mitad del guion en Manos libres pulsa F5: vuelven el guion y la posición y la UI no queda en "VOZ" sin escuchar. Luego cierra la pestaña y abre de nuevo http://127.0.0.1:8765. Repite cerrando y reabriendo la app nativa.

### 8. Micrófono desconectado
> Desconectar el micrófono durante la lectura: aparece un error visible en modo lectura y el modo pasa a pausa.

- Automático: busca en `tests/` los casos con `MIC_ERROR` y córrelos.
- Manual: en Manos libres desconecta el micrófono (USB o auricular) o deshabilítalo en Configuración > Sistema > Sonido > Entrada. Debe verse un aviso en modo lectura y el modo debe pasar a pausa. Reconéctalo y comprueba que Manos libres vuelve a arrancar.

### 9. Oculto en capturas y Siempre adelante
> Oculto en capturas: la ventana no aparece en la captura de ventana ni en la de pantalla de OBS, en Meet, Zoom o Teams al compartir pantalla, ni con Win+Shift+S. L lo alterna. T alterna "Siempre adelante" y la ventana se sigue moviendo y redimensionando con fluidez.

- Automático: en `launch.py`, `SetWindowDisplayAffinity` con `WDA_EXCLUDEFROMCAPTURE` (0x11) y su fallback; el topmost con `window.on_top` (no `SetWindowPos` desde otro hilo); las teclas L y T en `frontend/app.js`.
- Manual: con la app nativa abierta y "Oculto en capturas" activo: (a) OBS, captura de ventana y captura de pantalla: no aparece; (b) Meet, Zoom o Teams compartiendo la pantalla completa: no aparece; (c) Win+Shift+S: no aparece; (d) L la vuelve visible y L de nuevo la oculta; (e) T alterna "Siempre adelante" (compruébalo poniendo otra ventana encima); (f) mueve y redimensiona con T activo e inactivo: sin tirones ni congelamientos.

### 10. Instancia única
> Lanzar dos veces la app nativa: la segunda trae al frente la primera.

- Automático: con la app abierta, lanza otra vez `scripts\start_stealth.bat`, espera 5 s y cuenta los procesos de `launch.py` (debe haber 1) y los listeners del puerto 8765 (debe haber 1). Busca `CreateMutexW` en `launch.py`.
- Manual: confirma que la ventana existente pasó al frente y que no apareció otra ventana ni un error.

### 11. Cierre limpio
> Cerrar la ventana: `pythonw` termina y el puerto 8765 queda libre.

- Automático: tras cerrar (con la X o con `(Get-Process -Id <PID>).CloseMainWindow()`, que envía el mismo mensaje de cierre), sondea 10 s: sin procesos de `launch.py` y sin listener en el puerto 8765. Revisa las últimas líneas de `logs\teleprompter.log` (sin tracebacks).
- Manual: cierra con la X y confirma en el Administrador de tareas (pestaña Detalles) que no queda ningún `pythonw.exe` de la app.

### 12. Sin internet
> Sin internet: todo funciona y DevTools no muestra ninguna petición externa.

- Automático:
  - `Grep` de `https?://` en `frontend/`: solo `127.0.0.1` o `localhost` (se excluye el texto de licencias de fuentes).
  - `Grep` de `requests`, `httpx`, `urllib`, `urlopen`, `snapshot_download` y `hf_hub_download` en `backend/`, `tools/` y `launch.py`: nada en tiempo de ejecución (solo se admite en `tools/download_model.py`).
  - `local_files_only=True` y `HF_HUB_OFFLINE=1` presentes en la ruta de carga del modelo.
  - Con el backend corriendo y el modelo listo, sin conexiones remotas del proceso:
    ```powershell
    Get-NetTCPConnection -OwningProcess <PID> | Where-Object { $_.RemoteAddress -notin '127.0.0.1','::1','0.0.0.0','::' }
    ```
- Manual: desconecta la red (Wi-Fi apagado, modo avión o cable fuera), arranca la app en modo navegador y nativo, carga un guion y prueba Auto-scroll y Manos libres. En el navegador abre DevTools (F12) > Red, marca "Deshabilitar caché" y recarga: todas las peticiones van a `127.0.0.1:8765`, fuentes incluidas. Los procesos `msedgewebview2` pueden abrir conexiones propias del runtime (actualizaciones, SmartScreen): anótalas aparte, no son peticiones de la app.

### 13. Origen ajeno rechazado
> Una página de otro origen (por ejemplo, servida con `python -m http.server 9000`) que intenta abrir el WebSocket es rechazada.

- Automático: sonda `origen`: `127.0.0.1:8765` y `localhost:8765` aceptados; `127.0.0.1:9000` y `https://ejemplo.invalid` rechazados (HTTP 403 en el handshake o cierre 1008). Corre los tests de `Origin` de `tests/`. Comprueba que el rechazo queda en el log.
- Manual (navegador real): en una carpeta temporal fuera del repo crea `index.html` con `<script>const ws = new WebSocket("ws://127.0.0.1:8765/ws"); ws.onopen = () => document.title = "ABIERTO"; ws.onclose = (e) => document.title = "CERRADO " + e.code;</script>`, sírvela con `.venv312\Scripts\python.exe -m http.server 9000 --bind 127.0.0.1` desde esa carpeta y abre http://127.0.0.1:9000. El título debe quedar en "CERRADO 1008" (o 1006 si se rechaza en el handshake), nunca "ABIERTO". Detén el servidor al terminar.

### 14. Atajos con foco en controles
> Con el foco en un slider, los atajos no disparan acciones accidentales.

- Automático: en `frontend/app.js` debe haber un solo listener `keydown` (`Grep` de `addEventListener\(\s*["']keydown`) y el filtro de foco debe cubrir `INPUT`, `TEXTAREA`, `SELECT` e `isContentEditable`.
- Manual: en el setup haz clic en el slider de tamaño (o llega con Tab) y pulsa Espacio, ←, →, ↑, ↓, L, T, D, + y -: solo cambia el valor del slider con las flechas; no cambia el modo, no arranca nada y no alterna ventanas. Repite en el área de texto y en el selector de dispositivo (si existe).

### 15. Persistencia
> Al reiniciar la app se conservan el último guion, la posición y los ajustes.

- Automático: `.venv312\Scripts\python.exe -m pytest -q tests/test_api.py`. Si existe `data\settings.json`, muestra solo sus claves (no el texto del guion): `(Get-Content data\settings.json -Raw | ConvertFrom-Json).PSObject.Properties.Name`.
- Manual: carga un guion, avanza hasta la mitad, cambia tamaño de letra, interlineado y velocidad; cierra la app (nativa y modo navegador) y vuelve a abrirla: mismo guion, misma posición y mismos ajustes.

## Registro en progress.md

Al final (o con lo que haya si el usuario corta antes), lee `progress.md` y agrega al final, sin editar secciones anteriores:

```markdown
## QA AAAA-MM-DD

- Rama y commit: `fix/pulido-integral` @ `abc1234`. Entorno: `.venv312` (Python 3.12). Tests: N pasados, M fallados.

| # | Ítem | Automático | Manual | Resultado | Notas |
|---|---|---|---|---|---|
| 1 | Arranque `start.bat` | Puerto en 0,4 s; modelo listo en 21 s | OK | OK | |
```

- Fecha con `Get-Date -Format yyyy-MM-dd`. Si ya existe una sección "QA" con la fecha de hoy, agrega debajo una subsección `### Corrida HH:mm` en vez de repetir el título.
- Resultado: OK, FALLA, PARCIAL (una de las dos partes pasa y la otra falta o falla), PENDIENTE o N/A (no implementado todavía; indica la fase que lo implementa).
- Notas breves y concretas: cifras medidas, código de error y hallazgo relacionado (C1, A7, etc.). En español y sin guiones largos.
- Cierra con un resumen en el chat: cuántos OK, FALLA, PARCIAL y PENDIENTE, y la lista de fallos con el siguiente paso propuesto.
