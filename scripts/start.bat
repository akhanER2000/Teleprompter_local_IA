@echo off
setlocal
cd /d "%~dp0\.."

if not exist .venv\Scripts\activate.bat (
  echo [X] Entorno virtual no encontrado. Ejecuta scripts\install.bat primero.
  exit /b 1
)

call .venv\Scripts\activate.bat

set TP_MODEL_SIZE=medium
set TP_DEVICE=cuda
set TP_COMPUTE=float16
set TP_LANGUAGE=es

echo === Lanzando backend en http://127.0.0.1:8765 ===
start "" http://127.0.0.1:8765
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8765

endlocal
