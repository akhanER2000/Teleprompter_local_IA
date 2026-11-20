@echo off
REM ---------------------------------------------------------------
REM  Wrapper batch - delega en start_stealth.vbs para que la CMD
REM  NO quede visible. La consola que ejecuta este .bat se cierra
REM  inmediatamente tras lanzar el VBScript.
REM ---------------------------------------------------------------
cd /d "%~dp0\.."

if not exist ".venv312\Scripts\pythonw.exe" (
  echo [X] Falta el entorno .venv312, ver CLAUDE.md.
  pause
  exit /b 1
)

REM Lanzar via wscript (sin ventana de consola). /B evita el flash.
start "" /B wscript.exe "%~dp0start_stealth.vbs"

REM Cierre inmediato del CMD que ejecuto este .bat.
exit
