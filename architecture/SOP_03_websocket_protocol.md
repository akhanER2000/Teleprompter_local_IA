# SOP 03 — Protocolo WebSocket

## Endpoint
`ws://127.0.0.1:8765/ws`

## Cliente → Servidor (acciones)
```json
{"action": "load_script", "text": "Buenas tardes a todos..."}
{"action": "start"}
{"action": "pause"}
{"action": "reset"}
{"action": "set_position", "idx": 42}
```

## Servidor → Cliente (eventos)

### scroll
```json
{"type": "scroll", "current_idx": 42, "matched_word": "hola", "confidence": 0.91, "latency_ms": 187}
```

### transcript
```json
{"type": "transcript", "text": "buenas tardes a todos", "is_final": false}
```

### status
```json
{"type": "status", "state": "ready|listening|paused|error", "message": "..."}
```

## Reglas
- Single-client: solo una conexión activa. La segunda se rechaza con código 1008.
- Heartbeat: el servidor envía `{"type":"status","state":"listening"}` cada 5 seg si no hay scroll.
- Reconexión: el cliente reintenta con backoff exponencial (1s, 2s, 4s, máx 30s).
- Orden: los mensajes `scroll` llegan en orden de `current_idx` monotónico creciente.
