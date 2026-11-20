# Hito 85 · Vista con referencias

Especificación: `Profiles_StreamDeck/kit_Galleon100SD/docs/plan85/DECISIONES_85.md` (R1 a R6).

## Cómo escribir una referencia

Una por línea o en medio del texto, entre corchetes, como las directivas. La voz y el matcher
las ignoran; no son secciones.

| Escritura | Qué muestra |
|---|---|
| `[img: portada.png]` | la imagen (png, jpg, webp, gif; de un gif, el primer cuadro) |
| `[ppt: informe.pptx#5]` | la diapositiva 5 (sin `#`, la 1) |
| `[pdf: informe.pdf#3]` | la página 3 (sin `#`, la 1) |
| `[code: app.py#L10-L25]` | esas líneas, coloreadas (`#L7` una sola; sin `#`, las primeras 80) |
| `[nota: Mostrar el gráfico]` | una tarjeta de texto |

Los archivos se buscan en `guiones/<guion>_recursos/`, después en `guiones/` y por último como
ruta absoluta. Un archivo que no está, o un fragmento que no se entiende, deja la tarjeta en
grafito con «No encontrado: nombre» y la lectura sigue. El prefijo no distingue mayúsculas:
un `[Nota: …]` que antes era una directiva ahora es una referencia.

## Motores (R4), todo local

| Tipo | Motor en esta máquina | Notas |
|---|---|---|
| Diapositivas | **LibreOffice** sin ventana (`J:\DevToolsApps\LibreOffice`, hallado por el registro COM) | PowerPoint no está instalado. Si lo estuviera, se usa primero, por automatización con `cscript` (sin probar aquí). La presentación se convierte a PDF una vez y cada diapositiva se dibuja desde ese PDF. Sin ninguno de los dos: «Exporta la diapositiva como imagen: nombre#N». |
| PDF | **pypdfium2 4.30.0** (PDFium) | instalado en `.venv312` y fijado en `requirements.txt` y el lock |
| Imágenes | **Pillow 11.0.0** | ídem |
| Código | **Pygments 2.21.0** | ya estaba como dependencia de pytest; ahora fijado como dependencia directa. Colores en `frontend/tema.css`, sin CDN |

Caché: `guiones/.cache/`, ignorada por git. La clave lleva la ruta, el `mtime_ns`, el tamaño y
`THUMB_GEN` (`tools/ref_thumbs.py`, súbelo a mano si cambia cómo se dibuja). Nada de la caché se
borra: una entrada vieja deja de pedirse.

## Vista (R1, R3)

- **Solo texto** (por defecto) y **Con referencias**: tecla `V`, la orden `view` y la
  configuración. Se recuerda entre sesiones (`localStorage`, `tp.prefs.v3`).
- Tarjeta con borde de oro fino y etiqueta (`PPT · informe.pptx #5`), en la columna derecha a la
  altura de su renglón: el texto deja libre esa columna, así que nunca lo tapa.
- Tamaño chica, mediana o grande (15, 25 o 35 % del ancho); opacidad 40 a 100 %, 85 % con
  «Sobre presentación».
- Anuncio: 12 palabras antes, la tarjeta se enciende (`▸` en la etiqueta y borde claro) y la
  barra muestra `… · Z ampliar`.
- Ampliar: tecla `Z` u orden `ref_zoom`. Se cierra a los 8 s o al volver a pulsar.

## Contrato de control (R5, se suma a T6)

```
{"v":1,"cmd":"view","value":"texto"|"referencias"|"siguiente"}
{"v":1,"cmd":"ref_zoom"}
estado: ..., "vista":"texto"|"referencias", "ref_actual":"tipo: nombre"|null, "ref_ampliada":bool
```

`ref_actual` no es null mientras haya una referencia cerca (12 palabras antes hasta 40
después), en las dos vistas: es lo que habilita «Ampliar» en el Stream Deck.

## Validación

- `.venv312\Scripts\python.exe -m pytest -q`: 728 pasan: las 694 de antes, 3 de `tests/test_entorno.py` por las dependencias fijadas y 31 nuevas en
  `tests/test_referencias.py`, `tests/test_e2e_referencias.py` y
  `tests/frontend/referencias.test.js` (estas, dentro de `test_frontend.py`).
- `.venv312\Scripts\python.exe tools\sabotajes_85.py`: 17 sabotajes, los 17 en rojo (exit 1),
  restaurados por SHA-256, y vuelta a verde. Cada corrida usa su propia carpeta de bytecode:
  sin eso, el sabotaje de `pdf` dio verde con el `.pyc` del sabotaje anterior.
- `.venv312\Scripts\python.exe tools\capturas_85.py docs\hito85\capturas`: capturas y
  `metricas.json`; ninguna tarjeta tapa una palabra, sin scroll horizontal, sin errores de
  JavaScript, ninguna petición fuera de 127.0.0.1.
- Recursos del ejemplo: `tools\generar_ejemplo_referencias.py` (no sobrescribe).

## Capturas (`capturas/`)

| Archivo | Qué es |
|---|---|
| `h85_texto_400.png`, `h85_texto_1200.png` | lectura en Solo texto |
| `h85_referencias_400.png`, `h85_referencias_1200.png` | lectura Con referencias (la diapositiva se anuncia) |
| `h85_config_*` | configuración con los controles nuevos, en cada vista y ancho |
| `h85_ampliar_1200.png` | Ampliar sobre la diapositiva |
| `h85_sobre_negra_*.png`, `h85_sobre_blanca_*.png` | Sobre presentación compuesta sobre una diapositiva negra y una blanca (`*_alfa.png`: la captura transparente) |
