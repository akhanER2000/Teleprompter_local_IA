# SOP 01 — Pipeline de Audio

## Propósito
Capturar audio del micrófono en streaming continuo, segmentarlo en chunks listos para STT y encolarlos para consumo asíncrono.

## Contrato
- **Entrada:** dispositivo de audio del sistema (default).
- **Salida:** `asyncio.Queue` de objetos `AudioChunk { samples: np.ndarray[float32], timestamp_ms: int }`.

## Flujo
1. `sounddevice.InputStream` abre un stream a 16000 Hz mono float32 con callback.
2. El callback acumula samples hasta completar **8000 samples (500 ms)**.
3. Se aplica un VAD opcional (energía RMS > umbral) para descartar silencio puro y ahorrar GPU.
4. El chunk se push-ea a la cola. Si la cola está llena (> 4 elementos), se descarta el más antiguo — la frescura importa más que la integridad.

## Parámetros
| Param | Valor | Comentario |
|-------|-------|------------|
| sample_rate | 16000 | Tasa nativa de Whisper |
| channels | 1 | Mono |
| dtype | float32 | Whisper espera float32 normalizado |
| chunk_ms | 500 | Sweet spot latencia vs precisión |
| queue_max | 4 | Antibackpressure |
| rms_threshold | 0.005 | Por debajo = silencio |

## Errores
- Dispositivo no encontrado → estado `error`, mensaje claro al frontend.
- Subdesbordamiento del buffer → log warning, continúa.
