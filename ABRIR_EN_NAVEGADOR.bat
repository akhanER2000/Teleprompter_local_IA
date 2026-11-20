@echo off
REM Abre el teleprompter en el navegador (http://127.0.0.1:8765). Cierra esta ventana para apagarlo.
cd /d "%~dp0"
if not exist ".venv312\Scripts\python.exe" (
  echo [X] Falta el entorno .venv312. Ver CLAUDE.md, seccion "Como correr".
  pause
  exit /b 1
)
".venv312\Scripts\python.exe" scripts\abrir_navegador.py
