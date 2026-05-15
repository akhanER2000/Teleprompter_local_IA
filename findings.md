# FINDINGS — Restricciones y Descubrimientos

## Audio
- **Sample rate objetivo:** 16000 Hz mono float32. Whisper espera esta tasa; resampleo en backend si la tarjeta entrega 44.1/48 kHz.
- **Tamaño de chunk:** 500 ms (8000 samples). Trade-off: chunks más cortos reducen latencia pero degradan precisión de Whisper. 500 ms es el sweet-spot documentado de faster-whisper para streaming.
- **VAD (Voice Activity Detection):** faster-whisper incluye Silero VAD nativo (`vad_filter=True`). Evita transcribir silencios — crucial para no avanzar el scroll cuando el usuario respira.

## STT (faster-whisper)
- **Modelo:** `large-v3` ocupa ~3.1 GB VRAM en float16. `medium` ocupa ~1.5 GB. La RTX 5070 TI (16 GB VRAM) deja sobra para Ollama en paralelo.
- **Compute type:** `float16` en GPU. `int8_float16` si se quiere reducir aún más.
- **CUDA:** requiere `cuBLAS` y `cuDNN` del paquete `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`. faster-whisper >= 1.0 los detecta automáticamente.
- **Idioma:** forzar `language="es"` mejora latencia y precisión vs. auto-detect.

## Matching
- **Algoritmo elegido:** Sliding-window con normalización Unicode (NFD + strip combining + lowercase). Ventana = (current_idx − 5, current_idx + 20). Avance solo hacia adelante (monotonicidad) excepto si confidence > 0.95 permite saltos.
- **Por qué no Levenshtein global:** O(n*m) prohibitivo para guiones largos (>1000 palabras) en cada chunk.
- **Por qué no forced alignment (CTC):** Whisper no expone logits CTC fácilmente; el matching por superficie es suficiente para autoscroll.

## WebSocket
- **Puerto:** 8765 (no colisiona con dev servers comunes 3000/5000/8000/8080).
- **Una sola conexión:** El backend asume un único cliente (single-user, single-display).
- **Backpressure:** Si el frontend no consume, se descartan mensajes `scroll` antiguos (solo el último importa).

## Pantalla Dual
- **No requiere Electron:** Chrome/Edge soportan abrir ventana en monitor específico con `window.open(url, '_blank')` + arrastrar manual. Para automatización total se usaría la Window Management API (`window.getScreenDetails()`), pero requiere HTTPS o flag dev — fuera de scope.
- **Modo lectura:** F11 fullscreen en la ventana del segundo monitor.

## Riesgos identificados
1. **Cuello de botella audio→STT:** Si el chunk tarda > 500 ms en transcribirse, se acumula lag. Mitigación: usar `medium` si `large-v3` no rinde.
2. **Acentos / muletillas:** "este…", "eh…" pueden confundir matching. VAD ayuda pero no elimina.
3. **Drift acumulado:** Si el matching pierde sincronía, el scroll se descalibra. Mitigación: hotkey de "resync manual" (no implementado en MVP, pendiente).
