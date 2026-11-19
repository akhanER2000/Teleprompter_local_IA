# Clases y variables propuestas para el tema v3 (VD, hito 84)

Propuesta de VD mientras `frontend/CLASES.md` no existe. **Manda `CLASES.md` de VA**: si VA fija otros nombres, `tema.css` se adapta a los suyos.

## Reparto entre los dos archivos

- `styles.css` (VA): estructura, posición, tamaños que dependen de la ventana (T2), visibilidad, lógica de estados.
- `tema.css` (VD): se carga **después** de `styles.css` y solo pone piel. Define variables (`--tp-*`) y aplica colores, tipografía, bordes, sombras, banda y halo a las clases de abajo. No posiciona nada ni oculta nada.

## Lo que escribe la lógica (VA)

En `<body>`, como atributos de datos (el tema solo los lee):

| Atributo | Valores | Para qué |
|---|---|---|
| `class` | `mode-setup`, `mode-reader` (como hoy) | vista |
| `data-modo` | `manual`, `asistido`, `voz` | color del modo activo |
| `data-marcha` | `corriendo`, `pausa`, `espera`, `cuenta` | `espera`: Asistido parado porque callaste |
| `data-banda` | `on`, `off` | banda de lectura |
| `data-halo` | `on`, `off` | contorno oscuro de las letras |
| `data-texto` | `blanco`, `marfil`, `oro` | color del texto de lectura |

Como variables CSS en línea (sobre `body` o `:root`):

| Variable | Rango | Nota |
|---|---|---|
| `--tp-fondo-alfa` | 0 a 1 | `fondo_pct / 100`; el texto no la usa nunca |
| `--tp-progreso` | 0 a 1 | relleno de la barra de progreso |

## Clases

Las que ya existen se conservan: `.panel`, `.reader`, `.script-view`, `.reticle`, `.para`, `.w` con `.read`, `.current`, `.upcoming`, `.skip`, `.directive`, `.notice` con `.notice-info`, `.notice-warning`, `.notice-error`, `.notice-msg`, `.notice-action`, `.notice-close`, `.hidden`.

Nuevas:

| Clase | Qué es |
|---|---|
| `.barra` | franja de estado de la lectura, en su propia fila (no encima del texto) |
| `.barra-modo` | grupo Manual, Asistido y Voz; cada botón con `data-modo` y `aria-pressed="true"` el activo |
| `.barra-marcha` | estado: corriendo, pausa, esperando voz, cuenta atrás |
| `.barra-seccion` | nombre de la sección actual (`[LÁMINA …]`) |
| `.barra-velocidad` | velocidad (1 a 20) y palabras por minuto |
| `.barra-tiempo` | transcurrido y restante |
| `.barra-progreso` y `.barra-progreso-relleno` | riel de progreso; el ancho del relleno sale de `--tp-progreso` |
| `.chip` con `data-nivel="ok|aviso|error"` | indicadores pequeños: conexión, micrófono, modelo, sincronía |
| `.banda-lectura` | rectángulo detrás de las líneas actuales (VA lo posiciona) |
| `.cuenta-atras` y `.cuenta-num` | capa de cuenta atrás y su número |
| `.panel-seccion`, `.panel-titulo` | bloques del panel de configuración |
| `.campo` | una fila etiqueta + control |
| `.segmentado` | grupo de botones excluyentes (`aria-pressed`) |
| `.interruptor` | casilla con aspecto de interruptor (`input[type=checkbox]`) |
| `.muestra-color` con `data-color="blanco|marfil|oro"` | elegir color del texto |
| `.btn-primario`, `.btn-secundario` | jerarquía de botones |
| `.preajuste` | botón «Sobre presentación» |
| `.notice` con `data-tema="conexion|micro|modelo"` | ícono y acento del aviso |

## Variables del tema (las define `tema.css`)

Obsidiana y oro (los mismos valores que la colección «Galleon» de los Stream Deck): `--tp-obsidiana-alto` `#1C1E22`, `--tp-obsidiana-bajo` `#08090A`, `--tp-oro` `#D9B04C`, `--tp-oro-claro` `#F2D88C`, `--tp-oro-palido` `#F7E8B4`, `--tp-oro-oscuro` `#9C7322`.

Lectura (neutra): `--tp-lectura-fondo-rgb`, `--tp-lectura-texto`, `--tp-lectura-leida`, `--tp-lectura-marca`, `--tp-banda`, `--tp-halo`.

Tipografía, todas locales, sin red: `--tp-fuente-lectura`, `--tp-fuente-ui`, `--tp-fuente-mono`.
