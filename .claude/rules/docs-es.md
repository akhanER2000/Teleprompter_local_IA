---
paths: ["**/*.md"]
---

# Documentación en español

## Idioma y estilo

- Toda la documentación del proyecto va en español: README, `CLAUDE.md`, `architecture/SOP_*`, `progress.md`, `findings.md`, `task_plan.md`, `gemini.md` y los archivos de `.claude/` (commands, rules, agents, `skills/README.md`).
- Prohibido el guion largo (U+2014) y el guion medio (U+2013) como separador de incisos. Usa comas, paréntesis o dos puntos. El guion simple (`-`) de listas, rangos y palabras compuestas está permitido.
- Si el carácter tiene que aparecer como dato (por ejemplo, un token de prueba del parser), nómbralo en prosa ("guion largo (U+2014)") o escríbelo como escape (`\u2014`) dentro de código.
- Antes de terminar, busca ambos caracteres en los `.md` que tocaste (`Grep` con el patrón `[\x{2013}\x{2014}]`) y corrige cada aparición. Si el archivo ya tenía alguno, corrígelo también.
- Tildes y eñes correctas, en UTF-8. Los identificadores, comandos, rutas y nombres de API van en `código` y sin traducir.
- No se tocan: `PROMPT_MAESTRO_CORRECCIONES.md` (plan del usuario) ni las skills de terceros en `.claude/skills/*/` (salvo `.claude/skills/README.md`).

## Fidelidad al código

- La documentación describe el código real. Nunca se documentan funciones, atajos, variables `TP_*`, mensajes del protocolo ni KPI que no existan o no se hayan medido.
- Lo planificado se marca como tal ("pendiente", "planificado en la Fase N"), nunca como hecho.
- Si un cambio de código altera algo documentado (atajo, mensaje del protocolo, variable de configuración, comando), la documentación se actualiza en el mismo commit.
- Las cifras (latencias, métricas de replay, tamaños) salen de una medición registrada en `progress.md`, con fecha y configuración.

## progress.md

Al cerrar cada fase se agrega una entrada al final, sin reescribir las anteriores:

```markdown
## AAAA-MM-DD · Fase N: <título>

- Qué cambió: archivos y comportamientos principales.
- Por qué: hallazgos resueltos (C1, A3, M7...) y decisiones tomadas (D1...).
- Mediciones: tests (pasados/total), replay (p50, p95, saltos falsos, retrocesos, resync), tiempos de arranque u otras cifras de la fase.
- Pendiente y riesgos: lo que queda abierto y por qué.
```
