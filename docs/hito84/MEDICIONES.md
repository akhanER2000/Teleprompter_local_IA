# Hito 84 · Mediciones de VA (antes y después)

Todo medido en esta máquina el 2026-11-19, con el servidor real y Whisper `medium` en CUDA.

## T4 · Voz: la medición de V0, repetida

Los mismos audios que V0 (DIAGNOSTICO_84 §3c y §6), regenerados con `tools/synth_sample.py`
y `pausas2.py`; la referencia del `ejemplo` salió idéntica a la de V0. Cada audio se corrió dos
veces en la misma sesión con `tools/medir_voz.py`: **antes** = `start` sin modo (matcher v2 sin
frenos, lo de V0) y **después** = modo Voz con los frenos por defecto. Analizado con
`analizar_voz.py` de V0. Registros en `mediciones/` (los de OncoPath, solo índices y tiempos).

| `ejemplo`, 60 ppm con pausas | Antes | Después |
|---|---|---|
| Salto máximo en una emisión | 6 (saltos ≥ 4: 5) | **1** |
| Por delante de lo que con seguridad aún no empezó | 0 | 0 |
| Retraso por palabra p50 / p95 / máx | 0,66 / 2,53 / 5,7 s | 1,15 / **2,34** / 6,0 s |
| Llega al final | sí (89) | sí (89) |

| OncoPath, 63 ppm con pausas, 373 s | Antes | Después |
|---|---|---|
| Salto máximo en una emisión | 7 (saltos ≥ 4: 20) | **2** (4 veces) |
| Por delante de la cota | 3, máx +2 | 3, máx +2 (los mismos puntos: «de», «del») |
| Retraso por palabra p50 / p95 / máx | 0,65 / 2,09 / 7,7 s | 0,97 / **2,47** / 7,8 s |
| Llega al final | token 413 | token 413 |

Criterio de V0: «ninguna emisión por delante de la cota, ningún salto > 3, llega al final,
p95 ≤ 2,2 s». Cumple los tres primeros. **El p95 queda en 2,34 y 2,47 s**: es el precio de
confirmar antes de mover. Los frenos son configurables (confirmación, adelanto, velocidad).

Lo que se ajustó con estas mediciones (banco offline sobre los ciclos reales de Whisper):

- La velocidad máxima rendía 3,3 pal/s con 4,5 configurado (el resto se perdía en cada tick). Corregido; por defecto, 6 pal/s.
- La última palabra antes de una pausa esperaba al final de la pausa (en el silencio Whisper no devuelve nada que la respalde): +2,9 s de p95. Ahora una observación sólida que nadie contradice se confirma a los 0,6 s.
- El acuerdo de dos ciclos admite que el segundo pase al primero en 2 palabras.
- El tope de adelanto se mide desde la observación sólida más baja de los últimos 2 s: sin eso, en la lectura de prueba, dos completaciones de Whisper seguidas encadenaban 7–8 palabras de adelanto.

## T4 · Asistido, con micrófono simulado (`TP_AUDIO_FILE`)

30 s del audio `ejemplo`: 83 mensajes `activity`, 69 coinciden con la referencia (±0,3 s); el
servidor no movió el cursor (Asistido solo informa si hay voz; el visor avanza a velocidad fija).

## T2 · Ventana adaptable (`capturas/t2_*`, `capturas/metricas.json`)

Siete tamaños, configuración y lectura: ningún control fuera de la ventana, ningún scroll
horizontal, sin errores de JavaScript ni peticiones fuera de 127.0.0.1. Palabras por línea
(mediana): 3 a 400 px, 5 de 800 a 1600 × 900, 8–10 en ventanas bajas y anchas.

## T3 · Sobre presentación (`capturas/t3_<color>_<fondo>.png`)

Preajuste «Sobre presentación» (fondo 0 %, banda, halo), los tres colores sobre diapositiva
negra, blanca y una foto. **En la app nativa** WebView2 no deja transparencia por píxel (cinco
métodos medidos): el fondo baja el alfa de toda la ventana con piso del 50 %.

## Cómo repetir

```
.venv312\Scripts\python.exe -m pytest -q                  # 694 tests (e2e con Chrome si está)
.venv312\Scripts\python.exe tools\sabotajes_84.py         # 6 sabotajes en rojo y vuelta a verde
.venv312\Scripts\python.exe tools\capturas_v3.py docs\hito84\capturas
C:\Python314\python.exe tools\componer_t3.py docs\hito84\capturas
# voz: servidor con TP_AUDIO_FILE y luego
.venv312\Scripts\python.exe tools\medir_voz.py 8775 guiones\ejemplo.txt 98 salida.json voz   # o v2
```
