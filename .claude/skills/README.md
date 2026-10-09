# Skills del proyecto

Skills de terceros copiadas sin modificar desde `J:\Code\Claude_Skills` el 2026-10-09. No se editan aquí: las reglas de idioma y estilo del proyecto no se les aplican. Ante cualquier conflicto mandan las reglas del proyecto (`CLAUDE.md` y `.claude/rules/`).

Antes de copiarlas se revisaron sus `SKILL.md`, sus scripts (`find-polluter.sh` y `condition-based-waiting-example.ts` en `systematic-debugging`) y sus referencias: ninguna contiene instrucciones para enviar datos, contactar servicios externos de forma automática ni desactivar controles de seguridad.

| Skill | Origen en `J:\Code\Claude_Skills` | Por qué es relevante |
|---|---|---|
| `fastapi-expert` | `skills\06-backend-databases\fastapi-expert` | El backend es FastAPI 0.115 con el WebSocket `/ws`, lifespan y validación de payloads del protocolo v2. |
| `python-pro` | `skills\06-backend-databases\python-pro` | Código Python 3.12 tipado (dataclasses, logging, hilos) en `tools/` y `backend/`, y tests con pytest y fixtures. |
| `websocket-engineer` | `extras\06-backend-databases\websocket-engineer` | Protocolo v2: reconexión, cola de salida única, códigos de cierre 1008 y 4000 y validación de `Origin`. |
| `javascript-pro` | `extras\05-web-frontend\javascript-pro` | `frontend/app.js` vanilla: máquina de estados, WebSocket del navegador y animación con `requestAnimationFrame`. |
| `frontend-ui-engineering` | `skills\05-web-frontend\frontend-ui-engineering` | Accesibilidad, foco, estados de carga y error visibles en modo lectura (Fases 1 y 6). |
| `test-driven-development` | `skills\07-software-engineering\test-driven-development` | Tests de parser, matcher y protocolo escritos primero, sin GPU, sin micrófono y sin red. |
| `systematic-debugging` | `skills\07-software-engineering\systematic-debugging` | Diagnóstico por causa raíz de desfases de índice, condiciones de carrera (épocas) y cuelgues de arranque. |
| `security-and-hardening` | `skills\08-devops-security\security-and-hardening` | Fase 4: `Origin` del WebSocket, path traversal en la API de guiones y protección de la voz del usuario. |
| `documentation-and-adrs` | `extras\07-software-engineering\documentation-and-adrs` | Fase 7 y decisiones D1 a D5: README, SOP y registros de decisiones fieles al código. |
| `whisper` | `extras\02-ml-training-serving\whisper` | Parámetros de Whisper (idioma, `initial_prompt`, timestamps por palabra, alucinaciones) para el motor STT y el replay. |

## Cómo aplicarlas en este proyecto

- Los ejemplos con React, Tailwind, Socket.IO, Redis, Jest o npm son solo referencia conceptual: el frontend es vanilla sin build (`.claude/rules/frontend-vanilla.md`).
- `whisper` describe `openai-whisper` (requiere torch) y servicios en la nube. Este proyecto usa faster-whisper (CTranslate2) sin torch y 100% local: no instales `openai-whisper` ni torch y no sigas sus ejemplos con LangChain u OpenAI.
- `fastapi-expert` y `security-and-hardening` traen JWT, SQLAlchemy, cookies, CORS y rate limiting: no aplican a una app local de un solo usuario sin base de datos ni autenticación. Se aprovechan sus principios de validación en el borde y de manejo de datos sensibles.
- `python-pro` pide mypy estricto y cobertura mayor al 90%: aquí son orientativos, no un requisito.
- `frontend-ui-engineering` y `security-and-hardening` mencionan archivos `references/...` que no vienen en el origen. `systematic-debugging` menciona skills `superpowers:*` que no están instaladas, y su `find-polluter.sh` usa `npm test` (no aplica tal cual).
