@echo off
setlocal
cd /d "%~dp0\.."

if not exist .venv\Scripts\activate.bat (
  echo [X] Entorno virtual no encontrado. Ejecuta scripts\install.bat primero.
  exit /b 1
)

call .venv\Scripts\activate.bat

echo === Handshake CUDA ===
python tools\check_cuda.py
echo.
echo === Handshake Audio ===
python tools\check_audio.py
echo.
echo === Handshake Whisper ===
python tools\check_whisper.py

endlocal
