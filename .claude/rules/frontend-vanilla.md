---
paths: ["frontend/**"]
---

# Frontend vanilla

## Stack

- HTML, CSS y JavaScript vanilla. Sin frameworks ni librerías de UI (React, Vue, Svelte, jQuery, Alpine, Tailwind, etc.), sin TypeScript, sin build step, sin npm ni `package.json`, sin bundlers ni transpiladores.
- Ningún recurso externo: fuentes, iconos, imágenes y scripts viven en `frontend/` y se sirven desde `/static`. Inter va en `frontend/fonts/` (woff2 con su licencia OFL) o se usa una pila de fuentes del sistema. Sin `@import` remoto ni `preconnect` a otros hosts.
- `app.js` se mantiene como IIFE (o como módulo ES con `<script type="module">`) servido desde `/static/app.js`. No se agregan variables globales en `window`. El puente de pywebview (`window.pywebview.api`) se usa solo si existe, con un camino alternativo para el modo navegador.

## Responsabilidades

- El frontend solo dibuja y envía intenciones. El guion lo parsea el backend y llega como `segments` en `script_loaded`; no se reintroduce un parser propio en `app.js`.
- Los mensajes siguen el protocolo v2 (sección 8 del plan maestro y `architecture/SOP_03_websocket_protocol.md`): posiciones absolutas, `seq` monotónico y se ignora todo `scroll` con `epoch` menor al último `seq` enviado.
- La UI pasa a "VOZ" solo al recibir `status: listening`. Todo `status` de error se muestra en una zona visible también en modo lectura (barra o toast con `aria-live`).
- Si el WebSocket no está abierto, `send()` avisa al usuario en vez de descartar el mensaje en silencio.

## Teclado y accesibilidad

- Un único keymap y un único listener `keydown`. Los atajos se ignoran si el foco está en `input`, `textarea`, `select` o un elemento `[contenteditable]`.
- Al entrar en modo lectura se quita el foco de los botones.
- Cada atajo nuevo se agrega a la ayuda en pantalla y al README en el mismo commit.
- Controles con `<button>` y `<label>` reales, foco visible y contraste alto. Nada de `div` clicables sin rol ni teclado.
- El scroll suave propio (`requestAnimationFrame`) respeta `prefers-reduced-motion: reduce` con saltos instantáneos. No se usa `scroll-behavior: smooth` global en el CSS.

## Rendimiento y seguridad

- En cada `scroll` se actualizan clases solo en el rango que cambió; no se recorre todo el DOM.
- El texto del guion se inserta con `textContent` o nodos creados con `createElement`, nunca con `innerHTML` sobre contenido del usuario.
- Sin `eval`, `new Function` ni manejadores inline construidos con texto del usuario.
- Los textos de la interfaz van en español.
