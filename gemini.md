# GEMINI.md — Constitución del Proyecto Teleprompter Local IA

## 1. Identidad del Proyecto
- **Nombre:** Teleprompter Local IA ("Flow Local")
- **Objetivo:** Teleprompter con sincronización de voz en tiempo real, 100% local, optimizado para NVIDIA RTX 5070 TI.
- **Caso de uso crítico:** Operación "Manos Libres" — el texto dicta el ritmo, no al revés. El usuario debe poder operar la pantalla principal libremente mientras lee en una segunda pantalla.

## 2. Reglas de Comportamiento (Constitución)
1. **Ejecución 100% Local.** Ningún byte de audio, transcripción o texto del guion debe salir de la máquina. Prohibido cualquier endpoint en la nube.
2. **Determinismo sobre Probabilismo.** El enrutamiento entre capas (audio → STT → matching → scroll) es determinista. La IA no decide el flujo de control.
3. **Data-First.** El esquema de datos (Sección 4) es la verdad. Cambios al esquema obligan a actualizar arquitectura antes que código.
4. **Atomicidad.** Cada script en `tools/` hace UNA cosa. La composición vive en `backend/`.
5. **Latencia objetivo:** < 300 ms entre fin de palabra hablada y avance del scroll.
6. **Memoria persistente:** Toda decisión técnica relevante se registra en `progress.md`. Restricciones descubiertas en `findings.md`.

## 3. Stack Tecnológico
| Capa | Tecnología | Razón |
|------|-----------|-------|
| STT  | faster-whisper (CTranslate2) modelo `large-v3` o `medium` | Latencia milisegundos en GPU, no compite por VRAM con Ollama |
| NLP (opcional) | Ollama + Llama 3 8B / Mistral 7B | Pre-procesado de guion (split por pausas, normalización) |
| Backend | Python 3.11 + FastAPI + Uvicorn | Async nativo, WebSocket de primera clase |
| Transporte | WebSocket local `ws://127.0.0.1:8765/ws` | Push de palabra-índice sin polling |
| Audio | sounddevice + numpy | Captura PCM 16kHz mono directa |
| Frontend | HTML5 + CSS3 + JS vanilla (sin build step) | Cero fricción, scroll suave nativo, segunda pantalla con un click |
| Empaquetado | start.bat + venv | Doble click y listo |

## 4. DATA SCHEMA (Verdad Absoluta)

### 4.1 Entrada: Chunk de Audio (interno backend)
```json
{
  "chunk_id": 1234,
  "timestamp_ms": 17345,
  "samples": "<numpy float32 array, 16000 Hz mono, ~500 ms>",
  "sample_rate": 16000
}
```

### 4.2 Guion Cargado (in-memory en backend)
```json
{
  "script_id": "uuid-v4",
  "tokens": [
    {"idx": 0, "word": "Hola", "norm": "hola"},
    {"idx": 1, "word": "mundo", "norm": "mundo"},
    {"idx": 2, "word": ".",     "norm": ""}
  ],
  "total_tokens": 3
}
```
- `word` conserva mayúsculas/puntuación para render.
- `norm` es lowercase sin tildes ni puntuación, usado para matching.

### 4.3 Salida: Mensaje WebSocket Backend → Frontend
**Tipo `scroll`** (mensaje principal, dispara avance):
```json
{
  "type": "scroll",
  "current_idx": 42,
  "matched_word": "hola",
  "confidence": 0.91,
  "latency_ms": 187
}
```

**Tipo `transcript`** (debug/overlay, opcional):
```json
{
  "type": "transcript",
  "text": "hola mundo",
  "is_final": false
}
```

**Tipo `status`**:
```json
{
  "type": "status",
  "state": "ready | listening | paused | error",
  "message": "Modelo large-v3 cargado en CUDA"
}
```

### 4.4 Entrada: Mensaje WebSocket Frontend → Backend
```json
{"action": "load_script", "text": "<guion completo>"}
{"action": "start"}
{"action": "pause"}
{"action": "reset"}
{"action": "set_position", "idx": 0}
```

## 5. KPIs Innegociables
- [x] Ejecución 100% Local
- [x] Sincronización en Tiempo Real (< 300 ms)
- [x] Operación Manos Libres estricta
- [x] Optimización GPU RTX 5070 TI (CUDA + float16)
- [x] Doble Pantalla y Alta Legibilidad

## 6. Log de Mantenimiento
- 2026-05-15: Constitución inicializada. Esquema de datos congelado. Stack confirmado: FastAPI + faster-whisper + JS vanilla.
