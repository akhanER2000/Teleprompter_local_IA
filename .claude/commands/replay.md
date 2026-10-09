---
description: "Corre tools/replay.py sobre grabaciones WAV y compara las métricas con los objetivos de la Fase 2"
argument-hint: "[archivo.wav ...] [--script guion.txt] [--ref ref.json] [--step-ms 350] [--window-s 3.0] [--prompt-context] [--model medium]"
---

# /replay

Mide la sincronización por voz con grabaciones reales, sin tener que hablar. Argumentos recibidos: `$ARGUMENTS`

## Entradas

- Separa los argumentos en: rutas `.wav` y opciones de `replay.py` (`--script`, `--ref`, `--step-ms`, `--window-s`, `--prompt-context`, `--model`). Las opciones se aplican a todos los WAV.
- Sin WAV en los argumentos: usa todos los `*.wav` de `tests\fixtures\audio\`.
- Sin `--script`: empareja cada WAV con `guiones\ejemplo.txt`.
- `--ref` solo si el usuario lo pasa (si no, `replay.py` calcula la referencia con el alineamiento offline).
- `--model` distinto de `medium` (por ejemplo `large-v3-turbo`) solo si la decisión D2 está aprobada.

## Pasos

1. Comprueba que existe `tools\replay.py`. Si no existe, dilo (se construye en la Fase 2) y detente.
2. Lista los WAV (`Get-ChildItem tests\fixtures\audio\*.wav`). Si no hay ninguno, pide al usuario que grabe con `tools\record_sample.py`: lectura a ritmo normal, lectura saltando un párrafo y releyendo una frase, y 15 s de silencio o ruido.
3. Por cada WAV, uno por uno (cada corrida usa la GPU), con la herramienta PowerShell:

   ```powershell
   .venv312\Scripts\python.exe tools\replay.py --wav <archivo.wav> --script guiones\ejemplo.txt [--ref <ref.json>] [--step-ms 350] [--window-s 3.0] [--prompt-context] [--model medium] --csv tests\fixtures\audio\replay\<nombre>_<config>.csv
   ```

   - Los CSV derivados de la voz del usuario van solo en `tests\fixtures\audio\replay\` (ignorada por git). Nunca en una ruta versionada.
   - Si una corrida falla, informa el error en una línea y sigue con la siguiente.
4. Toma las métricas que imprime `replay.py` (o calcúlalas desde el CSV si solo exporta la línea de tiempo).

## Objetivos de la Fase 2

| Métrica | Objetivo |
|---|---|
| Retraso p50 (desde que se dice una palabra hasta que el índice la pasa) | ≤ 700 ms |
| Retraso p95 | ≤ 1500 ms |
| Saltos falsos (avances de más de 5 palabras que no son resincronización) | 0 |
| Retrocesos | 0 (si `replay.py` distingue los retrocesos por resincronización, infórmalos aparte) |
| Tiempo de resincronización tras saltarse un párrafo | ≤ 3 s |
| Avances con 15 s de silencio o ruido | ninguno |
| Cobertura del guion | informativa: compárala entre configuraciones |

## Informe

Una tabla por configuración, con una fila por WAV:

| WAV | p50 (ms) | p95 (ms) | Saltos falsos | Retrocesos | Cobertura | Resync (s) | Avances en silencio | Veredicto |
|---|---|---|---|---|---|---|---|---|

- Marca cada métrica como CUMPLE o NO CUMPLE. Aplica a cada grabación solo lo que corresponde: el WAV de silencio se juzga por "avances en silencio"; el de salto de párrafo, además, por el tiempo de resincronización.
- Si se compararon configuraciones (`--step-ms`, `--window-s`, `--prompt-context`, `--model`), agrega una tabla comparativa y recomienda una con números.
- Si un objetivo no se cumple, señala en qué tramo del CSV ocurre (tiempo e índice) y la hipótesis más probable. No cambies umbrales ni código desde este comando.
- Si el usuario pide registrar los resultados, agrégalos a `progress.md` (en español, sin guiones largos) con la fecha, el commit y la configuración usada.
- No copies transcripciones completas en el informe ni las envíes a ningún lado: contienen la voz del usuario.
