---
description: "Arranca el backend en modo navegador (en segundo plano) en http://127.0.0.1:8765 y espera a que responda"
argument-hint: "(sin argumentos)"
---

# /run

Arranca el backend en modo navegador para pruebas manuales.

## 1. Comprueba que el puerto 8765 esté libre

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    "{0}:{1} PID={2} {3}" -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess, $p.Path }
```

Si devuelve algo, **no arranques una segunda instancia**. Informa el PID y la ruta del proceso que ocupa el puerto (una instancia previa de `/run`, la app nativa o un `pythonw` huérfano) y pregunta qué hacer. No lo detengas sin confirmación.

## 2. Arranca el backend en segundo plano

Con la herramienta PowerShell y `run_in_background: true`, desde la raíz del repo:

```powershell
.venv312\Scripts\python.exe -m uvicorn backend.server:app --host 127.0.0.1 --port 8765
```

- Bind siempre en `127.0.0.1`, nunca `0.0.0.0`.
- No sobrescribas variables `TP_*`: respeta las que tenga definidas el usuario.
- No uses `scripts\start.bat` desde aquí: abre el navegador del usuario y deja una consola bloqueada.

## 3. Espera a que responda

Sondea hasta 120 s (si el modelo se carga antes de abrir el puerto, la primera respuesta puede tardar):

```powershell
$r = $null; $fin = (Get-Date).AddSeconds(120)
while (-not $r -and (Get-Date) -lt $fin) {
  try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/ }
  catch { Start-Sleep -Milliseconds 500 }
}
if ($r) { "OK: HTTP $($r.StatusCode)" } else { "Sin respuesta en 120 s" }
```

Si no responde: lee la salida del proceso en segundo plano y `logs\teleprompter.log` (si existe) e informa la causa real (modelo ausente, CUDA, import, puerto). No reintentes a ciegas.

## 4. Informa al usuario

- URL: http://127.0.0.1:8765 (ábrela en Edge o Chrome) y el PID del proceso.
- Estado del modelo si se puede saber (`loading`, `ready` o `error`), por la salida del proceso o el mensaje `state` del WebSocket.
- Prueba de humo sugerida: cargar `guiones\ejemplo.txt`, pulsar "Cargar guion", probar Auto-scroll y luego Manos libres.
- Para detenerlo: detén la tarea en segundo plano (o `Stop-Process -Id <PID>`) y confirma con el comando del paso 1 que el puerto quedó libre.
- Alternativa nativa: `scripts\start_stealth.bat` lanza `pythonw launch.py` (ventana pywebview sin consola). No la lances mientras esta instancia ocupe el puerto 8765.
