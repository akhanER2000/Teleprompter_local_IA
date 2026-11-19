# Diseño v3 · obsidiana y oro (hito 84, T5)

La cara nueva del teleprompter, diseñada en FigCraft y aplicada como `frontend/tema.css` sin tocar la lógica.

## Qué hay aquí

| Archivo | Qué es |
|---|---|
| `diseno_v3.figcraft` | el archivo de diseño (se abre en FigCraft) |
| `diseno_v3.json` | el guion de `figcraft-cli run` que lo construye |
| `gen_diseno_v3.py` | el generador: la fuente de la verdad del diseño |
| `png/` | las pantallas exportadas, a tamaño real |
| `SHA256SUMS.txt` | huellas del `.figcraft` y de cada PNG |
| `CLASES_PROPUESTAS.md` | clases y variables que lee el tema; VA las adoptó para `frontend/CLASES.md` |
| `validar_tema.py` | prueba en modo navegador: el tema carga sin errores y sin red |
| `capturas/` | la app real sin tema y con tema, y las comparaciones con el diseño |

## Pantallas (`png/`)

| PNG | Tamaño | Qué muestra |
|---|---|---|
| `lectura__manual.png` | 1600×900 | Manual en marcha, fondo opaco, banda con filete de oro |
| `lectura__pausa.png` | 1600×900 | pausa (riel en grafito), texto en oro con marca blanca |
| `lectura__asistido-sobre-presentacion.png` | 1600×900 | Asistido esperando la voz, preajuste «Sobre presentación» sobre una diapositiva blanca: fondo 0 %, banda y halo |
| `lectura__voz.png` | 1600×900 | Voz siguiendo, fondo al 45 % sobre una foto, nivel del micrófono y chip de sincronía |
| `lectura__cuenta-atras.png` | 1600×900 | cuenta atrás con aro de progreso |
| `avisos.png` | 1600×900 | avisos de conexión (error), micrófono (aviso) y modelo (info), con sus chips en la barra |
| `configuracion.png` | 1600×900 | panel: guion, lectura, fondo, avance con frenos de Voz, ventana y atajos |
| `tokens.png` | 1600×900 | paleta, tipografía y componentes |
| `compacta__lectura.png` | 400×700 | versión compacta: barra en dos filas, letra de 34 px |
| `compacta__avisos.png` | 400×700 | aviso en la versión compacta, con el botón debajo del texto |
| `compacta__configuracion.png` | 400×960 | configuración en una columna; frenos y ventana plegados |

Los números de la barra son de ejemplo, no son mediciones: salen de `guiones/ejemplo.txt` (89 palabras) a velocidad 6 (3 palabras por segundo, 180 ppm). Las secciones `[Lámina …]` y la diapositiva son de muestra.

## Decisiones

- **Obsidiana y oro** con los mismos valores que la colección «Galleon» de los Stream Deck (`#1C1E22`, `#08090A`, `#D9B04C`, `#F2D88C`, `#F7E8B4`, `#9C7322`). El oro marca lo activo: el modo, el progreso, la palabra actual, la sección.
- **La zona de lectura es neutra**: fondo `#0B0C0E`, texto en marfil `#F3EEE2` (o blanco, u oro), lo leído en `#76767D` (4,3:1). La palabra actual no cambia de color: lleva un **subrayado de oro**, que se lee igual con cualquiera de los tres colores (con texto en oro, el subrayado pasa a blanco).
- **Tipografía local**: Segoe UI Semibold para leer y para la interfaz, Consolas para las cifras. Vienen con Windows y se piden por nombre: ninguna petición de red.
- **La barra de estado va en su propia fila**, arriba, no encima del texto (V0, §3a). Modo, estado y tiempo a 20-24 px: se leen a 2 m.
- **Banda**: con fondo opaco, una franja apenas más clara con filete de oro; con fondo transparente, una franja oscura al 70 %. El CSS mezcla las dos según `--tp-fondo-alfa`.
- **Halo**: un contorno negro duro de 2 px más dos sombras suaves. Sobre la diapositiva blanca, el texto blanco se lee entero.
- **El texto entra y sale fundido** por arriba y por abajo, en vez de cortarse en seco.
- **Compacta (400 px)**: la barra pasa a dos filas (modo y tiempo restante; sección y velocidad); ningún rótulo se corta.

## Cómo se rehace

```
python docs\diseno_v3\gen_diseno_v3.py
```

Construye dos veces en carpetas nuevas con `figcraft-cli --no-system-fonts run`, exige el mismo SHA-256 en el `.figcraft` y en los 11 PNG, y los copia aquí. Necesita FigCraft en `J:\DevToolsApps\CraftingApps\FigCraft (Figma)\`, `fontTools` y las fuentes de Windows.

## Cómo se valida el tema

```
set TP_FAKE_STT=1 & set TP_PORT=8779 & .venv312\Scripts\python.exe -m uvicorn backend.server:app --host 127.0.0.1 --port 8779
.venv312\Scripts\python.exe -I docs\diseno_v3\validar_tema.py 8779 docs\diseno_v3\capturas
```

Exit 1 si `tema.css` no responde 200, no carga o no tiene reglas, si hay errores de consola o excepciones, o si la página pide algo fuera de `127.0.0.1` (salvo Google Fonts del `index.html` actual, que V0 ya marcó y la prueba bloquea). Si `index.html` aún no enlaza el tema, lo inyecta y lo dice; si la lógica aún no escribe los `data-*`, los pone con los valores del diseño y lo dice. Se vio en rojo: con una imagen inexistente en el tema, exit 1.
