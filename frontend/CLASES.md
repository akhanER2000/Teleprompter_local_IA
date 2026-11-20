# Contrato entre la lógica (VA) y el tema (VD)

`index.html` enlaza `/static/styles.css` (estructura y comportamiento) y **después**
`/static/tema.css` (aspecto, de VD). `app.js` solo escribe los atributos y variables
de esta lista; el tema puede vestir cualquiera de ellos. Si uno cambia de nombre, se
cambia aquí y en los dos lados a la vez.

## Atributos en `<body>` (los escribe `app.js`)

| Atributo | Valores | Qué dice |
|---|---|---|
| `class` | `mode-setup` · `mode-reader` | pantalla de configuración o de lectura |
| `data-modo` | `manual` · `asistido` · `voz` | modo de avance elegido |
| `data-marcha` | `corriendo` · `pausa` · `espera` · `cuenta` | `espera`: Asistido sin voz o Voz iniciando |
| `data-banda` | `on` · `off` | banda de lectura |
| `data-halo` | `on` · `off` | contorno oscuro en las letras |
| `data-texto` | `blanco` · `marfil` · `oro` | color del texto de lectura |
| `data-espejo` | `on` · `off` | lectura volteada (de `styles.css`) |
| `data-vista` | `texto` · `referencias` | hito 85: con `referencias` se ven las tarjetas |

## Variables CSS (en `:root`, las escribe `app.js`)

| Variable | Qué es |
|---|---|
| `--tp-fondo-alfa` | 0..1, opacidad del **fondo** (el texto no cambia) |
| `--tp-progreso` | 0..1, avance en el guion (barra fina) |
| `--tp-escala` | multiplicador de la letra (1..20 → 0,56..1,75) |
| `--tp-interlineado` | interlineado de la lectura |
| `--tp-linea` | alto real de un renglón en px (lo usa la banda) |
| `--tp-ref-pct` | hito 85: ancho de las tarjetas, 15, 25 o 35 (% del ancho) |
| `--tp-ref-opacidad` | hito 85: 0,4..1, opacidad de las tarjetas |

Hito 85, tarjetas de referencia: `ref-card[data-tipo=img|ppt|pdf|code|nota]` dentro de
`script-view`, con `ref-lejos` · `ref-proxima` (se anuncia) · `ref-actual` · `ref-leida`,
`ref-cargando` y `ref-error` (grafito). Dentro: `ref-etiqueta`, `ref-cuerpo` y, para el
código, `pre.ref-codigo` con spans `c-*` de Pygments. «Ampliar»: `ref-zoom` (con
`data-tipo` y `ref-error`), ids `ref-zoom-etiqueta` y `ref-zoom-cuerpo`. Chip `chip-ref`.

`styles.css` define además `--tp-barra-h`, `--tp-margen` y `--tp-letra-base`; el tema
aporta `--tp-halo`, `--tp-banda`, `--tp-texto-blanco/marfil/oro` y `--tp-lectura-leida`.

## Clases que usa el HTML

Panel: `panel`, `panel-cabecera`, `panel-marca`, `panel-sub`, `panel-acciones`,
`panel-cuerpo`, `panel-seccion`, `panel-titulo`, `panel-titulo-menor`, `campo`,
`campo-col`, `campo-etiqueta`, `hint`, `segmentado` (con `button[data-modo]` y
`aria-pressed`), `interruptor`, `muestra-color[data-color]` (con `aria-pressed`),
`colores`, `btn-primario`, `btn-secundario`, `preajuste`, `nativo` (solo app nativa),
`atajos`, `status[data-level]`, `notices`.

Lectura: `barra`, `barra-modo`, `barra-marcha`, `chips`, `chip[data-nivel=ok|aviso|error]`,
`barra-seccion`, `barra-velocidad`, `barra-tiempo`, `barra-progreso`,
`barra-progreso-relleno`, `lectura-area`, `banda-lectura`, `reticle`, `script-view`
(palabras `.w` con `.read`, `.current`, `.upcoming`), `preview-sample`, `preview-banner`,
`cuenta-atras`, `cuenta-num`, `hidden`.

Ids que usan las pruebas de VD: `btn-auto` (ahora «Empezar a leer»: arranca en el modo
elegido), `script`, `btn-load`.
