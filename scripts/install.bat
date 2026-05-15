@echo off
setlocal
cd /d "%~dp0\.."

echo === [1/4] Verificando Python ===
where python >nul 2>&1
if errorlevel 1 (
  echo [X] Python no encontrado en PATH. Instala Python 3.11+ y reintenta.
  exit /b 1
)

echo === [2/4] Creando entorno virtual .venv ===
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo === [3/4] Instalando PyTorch con soporte CUDA 12.1 ===
python -m pip install --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cu121

echo === [4/4] Instalando dependencias del proyecto ===
python -m pip install -r requirements.txt

echo.
echo === Instalacion completa ===
echo Ejecuta scripts\start.bat para lanzar el teleprompter.
endlocal
