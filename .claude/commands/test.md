---
description: "Corre la suite de pytest con el Python de .venv312 e informa los fallos de forma concisa"
argument-hint: "[ruta de test | -k expresión | opciones de pytest]"
---

# /test

Corre los tests del proyecto y resume el resultado. Argumentos recibidos: `$ARGUMENTS`

## Pasos

1. Desde la raíz del repo (`J:\Code\Teleprompter_local_IA`), con la herramienta PowerShell:

   ```powershell
   .venv312\Scripts\python.exe -m pytest -q $ARGUMENTS
   ```

   - Sin argumentos corre la suite completa (`tests/`).
   - Los argumentos se pasan tal cual a pytest: una ruta (`tests/test_parser.py`), `-k "matcher and not resync"`, `-x`, `-vv`, etc.
   - Usa siempre `.venv312\Scripts\python.exe`. Nunca el `python` del PATH ni `.venv` (está roto: su Python 3.9 base fue desinstalado).
   - Desde la herramienta Bash la ruta equivalente es `./.venv312/Scripts/python.exe`.
2. Si falla la importación de pytest o de un módulo del proyecto, informa el error tal cual y detente. No instales paquetes sin confirmación del usuario (y nunca torch).
3. Código de salida 5 (no se recolectó ningún test, por ejemplo si `tests/` aún no existe): dilo explícitamente. No lo cuentes como éxito ni como fallo.

## Informe

- Una línea de resumen: pasados, fallados, errores, omitidos y tiempo total.
- Por cada fallo: `archivo::test`, la aserción o excepción en una línea y, si es evidente, si la causa está en el código de producción o en el test.
- Agrupa los fallos con la misma causa raíz.
- No pegues tracebacks completos salvo que el usuario los pida.
- Si hay tests omitidos (`skip`/`xfail`), lista cuáles y su motivo.

## Reglas

- Nunca "arregles" un test debilitando sus aserciones, borrándolo, marcándolo `skip` o `xfail`, ampliando tolerancias o cambiando el valor esperado para que coincida con el resultado actual. Si un test parece incorrecto, explica por qué y pide confirmación antes de tocarlo.
- Este comando solo corre e informa. No modifiques código salvo que el usuario lo pida después.
- Los tests deben correr sin GPU, sin micrófono y sin red (STT falso inyectable, por ejemplo `TP_FAKE_STT=1`). Si alguno necesita GPU, micrófono, red o un modelo descargado, repórtalo como defecto del test.
