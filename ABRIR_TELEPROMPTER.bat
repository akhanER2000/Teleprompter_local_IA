@echo off
REM Abre el teleprompter como app nativa (invisible en capturas). Hito 84: base unica.
cd /d "%~dp0"
if not exist ".venv312\Scripts\pythonw.exe" (
  echo [X] Falta el entorno .venv312. Ver CLAUDE.md, seccion "Como correr".
  pause
  exit /b 1
)
start "" ".venv312\Scripts\pythonw.exe" launch.py
