# Teleprompter Local IA — "Flow Local"

Teleprompter con sincronización de voz en tiempo real y modo automático, **100% local**, optimizado para NVIDIA RTX (probado en RTX 5070 TI).

## Características Principales
- **Modo Manos Libres (Voz):** El texto avanza a medida que hablas. Usa el modelo Whisper (`faster-whisper`) para detectar tu voz localmente con mínima latencia.
- **Modo Auto-Scroll:** Teleprompter clásico con avance automático a velocidad ajustable.
- **Modo Stealth (App Nativa):** Ejecución en ventana independiente (vía `pywebview`) que permite transparencia y "protección" contra capturas de pantalla de la CMD.
- **Always on Top & Lock:** Mantén la ventana siempre al frente y bloquea la interacción para evitar clics accidentales mientras lees.
- **Directivas de Escena:** Todo texto encerrado entre corchetes (ej. `[Mostrar en pantalla: Fase 1]`) se renderiza como una directiva visual destacada y **es ignorado** por el reconocedor de voz.
- **Privacidad Total:** 100% de procesamiento offline. Ningún dato sale de tu equipo.

## Requisitos previos
1. **Windows 10/11** con NVIDIA RTX (drivers recientes).
2. **Python 3.9 - 3.11** en PATH (Nota: Python 3.14 es muy reciente y carece de compatibilidad con algunas dependencias compiladas de audio/ML).
3. **CUDA Toolkit 12.x** instalado.
4. **Micrófono** funcional.

## Instalación (una sola vez)
```bat
scripts\install.bat
```
Crea `.venv\`, instala PyTorch CUDA 12.1, FastAPI, faster-whisper, pywebview y todas las dependencias.

## Verificación de hardware (recomendado)
```bat
scripts\handshake.bat
```
Comprueba CUDA, micrófono y transcripción Whisper.

## Ejecución

### Modo Normal (Navegador)
```bat
scripts\start.bat
```
- Abre tu navegador en `http://127.0.0.1:8765`.
- Ideal para leer en una segunda pantalla.

### Modo Stealth (App Nativa)
```bat
scripts\start_stealth.bat
```
- Lanza una ventana nativa sin bordes de consola visibles.
- Soporta funciones de "Siempre adelante" y "Bloqueo de interacción".

## Atajos de Teclado
| Tecla | Acción |
|-------|--------|
| `Espacio` | Play / Pausa (inicia en modo Voz si está en idle) |
| `A` | Activar / Desactivar Auto-scroll |
| `↑` / `↓` | Subir / Bajar velocidad del Auto-scroll |
| `L` | **Stealth**: Bloquear/Desbloquear interacción (solo en App Nativa) |
| `T` | **Topmost**: Alternar "Siempre adelante" (solo en App Nativa) |
| `R` | Reiniciar el guion al principio |
| `F11` | Alternar pantalla completa |
| `Esc` | Volver al panel de configuración |

## Variables de entorno
| Variable | Default | Opciones |
|----------|---------|----------|
| `TP_MODEL_SIZE` | `medium` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `TP_DEVICE` | `cuda` | `cuda`, `cpu` |
| `TP_COMPUTE` | `float16` | `float16`, `int8_float16`, `int8`, `float32` |
| `TP_LANGUAGE` | `es` | código ISO 639-1 |

## Estructura
```
Teleprompter_local_IA/
├── gemini.md           # Constitución + Data Schema
├── task_plan.md        # Roadmap B.L.A.S.T.
├── findings.md         # Restricciones técnicas
├── progress.md         # Bitácora de avance
├── architecture/       # SOPs deterministas
├── tools/              # Scripts atómicos Python
├── backend/            # FastAPI + WebSocket
├── frontend/           # HTML/CSS/JS vanilla
├── scripts/            # .bat de instalación y lanzamiento
├── launch.py           # Entry point para App Nativa (pywebview)
└── requirements.txt
```

## Solución de problemas
- **"CUDA no disponible"** → reinstala drivers NVIDIA, verifica con `nvidia-smi`.
- **"Micrófono con RMS bajo"** → revisa el dispositivo predeterminado de Windows.
- **Latencia alta (>500 ms)** → cambia a `set TP_MODEL_SIZE=small`.
- **El scroll se desincroniza** → presiona `R` para reiniciar al principio.
