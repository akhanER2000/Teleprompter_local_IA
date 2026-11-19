"""Diseño v3 del teleprompter (hito 84, T5) en FigCraft, sin ventana.

    python docs/diseno_v3/gen_diseno_v3.py

Escribe `diseno_v3.json` (el guion de `figcraft-cli run`), lo corre dos veces en carpetas nuevas, exige el mismo
SHA-256 en el `.figcraft` y en cada PNG, y deja `diseno_v3.figcraft` y `png/<pantalla>.png` en esta carpeta.

Fuentes: las de Windows (Segoe UI, Segoe UI Semibold, Consolas), las mismas que usa `frontend/tema.css` por nombre
local. No se copian al repositorio ni se descargan.

Los números de la barra (velocidad, tiempos, progreso) son de ejemplo: salen del guion `guiones/ejemplo.txt`
(89 palabras) leído a velocidad 6 (3 palabras por segundo, 180 ppm). No son mediciones.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile

from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(HERE))
CLI = r'J:\DevToolsApps\CraftingApps\FigCraft (Figma)\figcraft-cli.exe'
FUENTES = r'C:\Windows\Fonts'
ARCH = {('Segoe UI', 'Regular'): 'segoeui.ttf', ('Segoe UI', 'Semibold'): 'seguisb.ttf',
        ('Consolas', 'Regular'): 'consola.ttf'}

# ── tokens (los mismos que tema.css) ──
OB_ALTO, OB_BAJO = '#1C1E22', '#08090A'          # colección «Galleon» de los Stream Deck
ORO, ORO_CLARO, ORO_PALIDO, ORO_OSCURO = '#D9B04C', '#F2D88C', '#F7E8B4', '#9C7322'
LECT_FONDO = '#0B0C0E'                            # zona de lectura: casi negro neutro
TEXTO = {'blanco': '#FFFFFF', 'marfil': '#F3EEE2', 'oro': '#F2D88C'}
LEIDA = '#76767D'                                 # lo ya leído: 4,3:1 sobre LECT_FONDO
UI_1, UI_2, UI_3 = '#ECE7DB', '#A8A397', '#6C6A64'
BORDE, PANEL, CAMPO = '#2E3036', '#121317', '#0E0F12'
OK, AVISO, ERROR = '#7DAD82', '#E0A84A', '#E06A55'
GRAFITO = '#8C8778'
GRAD_ORO = [ORO_CLARO, ORO]

# ── texto ──
_metr = {}
def _fuente(fam, est):
    k = (fam, est)
    if k not in _metr:
        t = TTFont(os.path.join(FUENTES, ARCH[k]))
        _metr[k] = (t.getBestCmap(), t['hmtx'], t['head'].unitsPerEm, t['hhea'].ascent, t['hhea'].descent,
                    t['hhea'].lineGap)
    return _metr[k]

def ancho(s, size, fam='Segoe UI', est='Semibold', ls=0.0):
    cmap, hmtx, upem, *_ = _fuente(fam, est)
    w = sum(hmtx[cmap.get(ord(c), cmap[ord('?')])][0] for c in s)
    return w * size / upem + ls * len(s)

def caja(size, fam='Segoe UI', est='Semibold'):
    """(alto de la caja de texto, distancia de su borde superior a la línea base)."""
    _, _, upem, asc, desc, gap = _fuente(fam, est)
    return (asc - desc + gap) * size / upem, asc * size / upem

def partir(texto, size, w, fam='Segoe UI', est='Semibold'):
    lineas, act = [], ''
    for p in texto.split():
        prueba = f'{act} {p}'.strip()
        if act and ancho(prueba, size, fam, est) > w:
            lineas.append(act); act = p
        else:
            act = prueba
    if act: lineas.append(act)
    return lineas

# ── el guion de figcraft-cli ──
class Doc:
    def __init__(self):
        self.steps, self.n = [], 0

    def _id(self, p):
        self.n += 1; return f'{p}{self.n}'

    def step(self, cmd, params, ref=None):
        s = {'command': cmd, 'params': params}
        if ref: s['as'] = ref
        self.steps.append(s)

    def frame(self, name, x, y, w, h, fills=(), parent=None, clip=True):
        r = self._id('f')
        p = {'type': 'frame', 'name': name, 'x': x, 'y': y, 'width': w, 'height': h,
             'props': {'fills': list(fills), 'clip': clip}, 'select': False}
        if parent: p['parent'] = {'$ref': parent}
        self.step('node.create', p, r); return r

    def rect(self, parent, name, x, y, w, h, fills=(), radius=0, stroke=None, sw=1, effects=None, tipo='rectangle',
             op=None):
        r = self._id('r')
        props: dict = {'fills': list(fills)}
        if op is not None: props['opacity'] = op
        if radius: props['cornerRadius'] = radius
        if stroke: props.update(strokes=[solido(stroke)], strokeWeight=sw, strokeAlign='inside')
        if effects: props['effects'] = effects
        self.step('node.create', {'type': tipo, 'name': name, 'parent': {'$ref': parent}, 'x': round(x, 2),
                                  'y': round(y, 2), 'width': round(w, 2), 'height': round(h, 2), 'props': props,
                                  'select': False}, r)
        return r

    def elipse(self, parent, name, x, y, w, h, fills=(), stroke=None, sw=1):
        return self.rect(parent, name, x, y, w, h, fills, stroke=stroke, sw=sw, tipo='ellipse')

    def vector(self, parent, name, path, x, y, w, h, fill=None, stroke=None, sw=2):
        r = self._id('v')
        props: dict = {'fills': [solido(fill)] if fill else []}
        if stroke: props.update(strokes=[solido(stroke)], strokeWeight=sw, strokeAlign='center', strokeCap='round',
                                strokeJoin='round')
        self.step('node.create', {'type': 'vector', 'name': name, 'parent': {'$ref': parent}, 'path': path,
                                  'x': x, 'y': y, 'width': w, 'height': h, 'props': props, 'select': False}, r)
        return r

    def texto(self, parent, s, x, y, size, color, est='Semibold', fam='Segoe UI', ls=0, alin='left', w=None,
              halo=False, nombre=None, mayus=False):
        """Una línea. `y` es el borde superior de la caja; `alin` con `w` centra o alinea a la derecha."""
        if mayus: s = s.upper()
        tw = ancho(s, size, fam, est, ls)
        if w is not None and alin == 'center': x = x + (w - tw) / 2
        elif w is not None and alin == 'right': x = x + w - tw
        r = self._id('t')
        props = {'fontFamily': fam, 'fontStyle': est, 'fontSize': size, 'fills': [solido(color)]}
        if ls: props['letterSpacing'] = ls
        if halo: props['effects'] = HALO
        self.step('text.create', {'text': s, 'x': round(x, 2), 'y': round(y, 2), 'parent': {'$ref': parent},
                                  'name': nombre or f'texto/{s[:24]}', 'select': False, 'props': props}, r)
        return r, tw

def solido(c, op=None):
    p = {'type': 'solid', 'color': c}
    if op is not None: p['opacity'] = op
    return p

def grad(stops, de=(0, 0), a=(0, 1)):
    return {'type': 'linearGradient', 'stops': [{'color': c, 'position': i / (len(stops) - 1)}
                                                 for i, c in enumerate(stops)], 'from': list(de), 'to': list(a)}

def sombra(color, radius, dy=0, spread=0):
    return {'type': 'dropShadow', 'color': color, 'offset': [0, dy], 'radius': radius, 'spread': spread}

# Halo: tres sombras sin desplazamiento, un contorno duro de 2 px y dos suaves (tema.css: --tp-halo).
HALO = [sombra('#000000', 1, spread=2), sombra('#000000D9', 4, spread=1), sombra('#00000080', 14)]

# ── glifos ──
G_PLAY = 'M2 1 L16 9 L2 17 Z'
G_PAUSA = 'M2 1 H7 V17 H2 Z M11 1 H16 V17 H11 Z'
G_MIC = 'M9 1 C11.2 1 12.5 2.6 12.5 4.5 V9 C12.5 11 11.2 12.5 9 12.5 C6.8 12.5 5.5 11 5.5 9 V4.5 C5.5 2.6 6.8 1 9 1 Z M2.5 8.5 C2.5 12.5 5.5 15.5 9 15.5 C12.5 15.5 15.5 12.5 15.5 8.5 M9 15.5 V18'
G_ENCHUFE = 'M6 1 V5 M12 1 V5 M3 5 H15 V9 C15 12.5 12.5 14.5 9 14.5 C5.5 14.5 3 12.5 3 9 Z M9 14.5 V18'
G_MODELO = 'M4 4 H14 V14 H4 Z M7 1 V4 M11 1 V4 M7 14 V17 M11 14 V17 M1 7 H4 M1 11 H4 M14 7 H17 M14 11 H17'
G_RELOJ = 'M9 1.5 A7.5 7.5 0 1 1 8.99 1.5 Z M9 5 V9 L12 11'

# ── el guion de ejemplo ──
def guion():
    with open(os.path.join(RAIZ, 'guiones', 'ejemplo.txt'), encoding='utf-8') as fh:
        paras = [p.strip() for p in fh.read().split('\n\n') if p.strip()]
    # Las directivas son de muestra, para enseñar cómo se ve una sección ([LÁMINA …]); no están en ejemplo.txt.
    secc = {1: 'Lámina 2 · Arquitectura', 2: 'Lámina 3 · Voz local', 3: 'Lámina 4 · Manos libres'}
    bloques = []
    for i, p in enumerate(paras):
        if i in secc: bloques.append(('dir', secc[i]))
        bloques.append(('para', p.split()))
    return bloques

N_PALABRAS = sum(len(b[1]) for b in guion() if b[0] == 'para')       # 89
VEL, PPS = 6, 3.0                                                    # velocidad 6 = 3 palabras/s = 180 ppm

def tiempos(idx):
    hecho, falta = idx / PPS, (N_PALABRAS - idx) / PPS
    f = lambda s: f'{int(s // 60)}:{int(round(s % 60)):02d}'
    return f(hecho), f'−{f(falta)}', idx / N_PALABRAS

# ── vista de lectura ──
def maquetar(bloques, size, lh, w):
    """Lista de líneas: ('dir', texto) o ('para', [(i_global, palabra), …], gap_antes)."""
    out, gi = [], 0
    for b in bloques:
        if b[0] == 'dir':
            out.append(('dir', b[1])); continue
        act, primera = [], True
        for p in b[1]:
            prueba = ' '.join(x for _, x in act + [(gi, p)])
            if act and ancho(prueba, size) > w:
                out.append(('para', act, primera)); act, primera = [], False
            act.append((gi, p)); gi += 1
        if act: out.append(('para', act, primera))
    return out

def lectura(d, padre, x0, y0, W, H, *, size, lh, margen, actual, texto='marfil', banda=True, halo=False,
            fondo=None, banda_op=0.7, foco=0.36, marca=ORO):
    """Zona de lectura: líneas leídas arriba, la banda en el foco, lo que sigue abajo."""
    z = d.frame('lectura', x0, y0, W, H, [solido(*fondo)] if fondo else [], parent=padre)
    tw = W - 2 * margen
    lineas = maquetar(guion(), size, lh, tw)
    alto_dir = round(size * 0.42 * 2.4)
    # línea que contiene la palabra actual
    k = next(i for i, l in enumerate(lineas) if l[0] == 'para' and any(g == actual for g, _ in l[1]))
    foco_y = H * foco
    opaco = fondo is not None and fondo[1] >= 1
    if banda:
        bh, bx = 2 * lh + 20, min(24, margen / 2.5)
        if not opaco:                            # sobre presentación: franja oscura translúcida
            d.rect(z, 'banda-lectura', bx, foco_y - 10, W - 2 * bx, bh, [solido(OB_BAJO, banda_op)], radius=16)
        else:                                    # opaco: franja apenas más clara, con su filete de oro
            d.rect(z, 'banda-lectura', bx, foco_y - 10, W - 2 * bx, bh, [solido('#1A1B20')], radius=16)
            d.rect(z, 'banda-filete', bx, foco_y - 10 + 18, 4, bh - 36, [solido(ORO)], radius=2)
    # alto de cada línea y aire antes de cada párrafo o sección; la línea actual cae en el foco
    alto = [alto_dir if l[0] == 'dir' else lh for l in lineas]
    aire = [0.35 * lh if i and (l[0] == 'dir' or l[2]) else 0 for i, l in enumerate(lineas)]
    pos, acc = [], 0.0
    for i in range(len(lineas)):
        acc += aire[i]; pos.append(acc); acc += alto[i]
    ys = {i: foco_y + pos[i] - pos[k] for i in range(len(lineas))}
    cbox, base = caja(size)
    col = TEXTO[texto]
    for i, l in enumerate(lineas):
        ly = ys[i]
        if ly < -lh or ly > H: continue
        ty = ly + (lh - cbox) / 2
        if l[0] == 'dir':
            dsize = round(size * 0.42)
            dy = ly + (alto_dir - caja(dsize)[0]) / 2
            leida = i < k
            d.rect(z, 'directiva-filete', margen, dy + 4, 4, caja(dsize)[0] - 8, [solido(ORO_OSCURO if leida else ORO)],
                   radius=2)
            d.texto(z, l[1], margen + 18, dy, dsize, ORO_OSCURO if leida else ORO_CLARO, ls=2.5, mayus=True,
                    halo=halo, nombre='directiva')
            continue
        palabras = l[1]
        # tramos: leído / actual / por leer
        tramos, x = [], margen
        leidas = [p for g, p in palabras if g < actual]
        act = [p for g, p in palabras if g == actual]
        resto = [p for g, p in palabras if g > actual]
        for grupo, color, rol in ((leidas, LEIDA, 'leida'), (act, col, 'actual'), (resto, col, 'por-leer')):
            if not grupo: continue
            s = ' '.join(grupo)
            _, w = d.texto(z, s, x, ty, size, color, halo=halo, nombre=f'palabras/{rol}')
            if rol == 'actual':
                d.rect(z, 'marca-actual', x - 2, ty + base + size * 0.13, w + 4, max(4, round(size * 0.075)),
                       [solido(marca)], radius=3, effects=[sombra('#000000AA', 4)] if halo else None)
            x += w + ancho(' ', size)
    if opaco:                                    # el texto entra y sale fundido, no cortado en seco
        fh = round(lh * 0.9)
        d.rect(z, 'fundido/arriba', 0, 0, W, fh, [grad([fondo[0], fondo[0] + '00'])])
        d.rect(z, 'fundido/abajo', 0, H - fh, W, fh, [grad([fondo[0] + '00', fondo[0]])])
    return z

# ── barra de estado ──
def segmentado(d, padre, x, y, h, size, activo, nombres=('Manual', 'Asistido', 'Voz'), pad=18):
    anchos = [ancho(n, size) + 2 * pad for n in nombres]
    W = sum(anchos) + 8
    d.rect(padre, 'barra-modo', x, y, W, h, [solido(CAMPO)], radius=h / 2, stroke=BORDE)
    cx = x + 4
    cb, _ = caja(size)
    for n, a in zip(nombres, anchos):
        on = n.lower() == activo
        if on: d.rect(padre, f'modo/{n}/activo', cx, y + 4, a, h - 8, [grad(GRAD_ORO)], radius=(h - 8) / 2)
        d.texto(padre, n, cx, y + (h - cb) / 2, size, '#141414' if on else UI_2, w=a, alin='center',
                nombre=f'modo/{n}')
        cx += a
    return W

def marcha(d, padre, x, y, size, estado):
    cb, _ = caja(size)
    g = size * 0.8
    gy = y + (cb - g) / 2
    if estado == 'corriendo':
        d.vector(padre, 'glifo/play', G_PLAY, x, gy, g * 0.85, g, fill=ORO)
        _, w = d.texto(padre, 'En marcha', x + g + 10, y, size, UI_1)
    elif estado == 'pausa':
        d.vector(padre, 'glifo/pausa', G_PAUSA, x, gy, g * 0.85, g, fill=ORO_CLARO)
        _, w = d.texto(padre, 'En pausa', x + g + 10, y, size, ORO_CLARO)
    elif estado == 'espera':
        d.vector(padre, 'glifo/mic', G_MIC, x, gy, g, g, stroke=AVISO, sw=2)
        _, w = d.texto(padre, 'Esperando tu voz', x + g + 10, y, size, AVISO)
    else:
        d.vector(padre, 'glifo/reloj', G_RELOJ, x, gy, g, g, stroke=ORO_CLARO, sw=2)
        _, w = d.texto(padre, 'Cuenta atrás', x + g + 10, y, size, ORO_CLARO)
    return g + 10 + w

def nivel_mic(d, padre, x, y, h, n_on, n=5):
    for i in range(n):
        bh = h * (0.35 + 0.65 * (i + 1) / n)
        d.rect(padre, f'mic/{i}', x + i * 7, y + h - bh, 4, bh, [solido(OK if i < n_on else BORDE)], radius=2)
    return n * 7

def barra(d, padre, W, *, modo, estado, actual, seccion, op=0.94, chips=(), mic=None):
    H = 76
    b = d.frame('barra', 0, 0, W, H + 4, [], parent=padre)
    d.rect(b, 'barra/fondo', 0, 0, W, H, [grad([OB_ALTO, OB_BAJO])], op=op)
    sw = segmentado(d, b, 24, 16, 44, 20, modo)
    x = 24 + sw + 28
    cb22, _ = caja(22)
    x += marcha(d, b, x, (H - cb22) / 2, 22, estado) + 24
    if mic is not None:
        nivel_mic(d, b, x, 26, 24, mic); x += 35 + 12
    for nombre, nivel in chips:
        x += chip(d, b, x, 20, nombre, nivel) + 10
    hecho, falta, prog = tiempos(actual)
    # derecha: tiempo y velocidad
    cbm, _ = caja(24, 'Consolas', 'Regular')
    r, wt = d.texto(b, f'{hecho}  {falta}', 0, (H - cbm) / 2, 24, UI_1, est='Regular', fam='Consolas',
                    w=W - 24, alin='right', nombre='barra-tiempo')
    xr = W - 24 - wt - 28
    s_vel = f'vel {VEL} · {int(PPS * 60)} ppm'
    _, wv = d.texto(b, s_vel, 0, (H - cbm) / 2, 24, ORO_CLARO, est='Regular', fam='Consolas', w=xr, alin='right',
                    nombre='barra-velocidad')
    xr -= wv + 28
    # centro: sección, en el hueco que quede
    cb18, _ = caja(18)
    ws = ancho(seccion.upper(), 18, ls=2.5)
    cx = x + (xr - x - ws) / 2
    d.texto(b, seccion, cx, (H - cb18) / 2, 18, ORO_CLARO, ls=2.5, mayus=True, nombre='barra-seccion')
    # riel de progreso
    d.rect(b, 'barra-progreso', 0, H, W, 4, [solido('#24252A')])
    d.rect(b, 'barra-progreso-relleno', 0, H, round(W * prog), 4,
           [grad(GRAD_ORO, (0, 0), (1, 0))] if estado != 'pausa' else [solido(GRAFITO)])
    return H + 4

def chip(d, padre, x, y, nombre, nivel, size=18):
    color = {'ok': OK, 'aviso': AVISO, 'error': ERROR}[nivel]
    cb, _ = caja(size)
    w = ancho(nombre, size) + 34
    d.rect(padre, f'chip/{nombre}', x, y, w, cb + 10, [solido(color, 0.12)], radius=(cb + 10) / 2, stroke=color)
    d.elipse(padre, 'chip/punto', x + 11, y + (cb + 10) / 2 - 4, 8, 8, [solido(color)])
    d.texto(padre, nombre, x + 25, y + 5, size, UI_1, nombre=f'chip/{nombre}')
    return w

# ── fondos de presentación (para ver la transparencia) ──
def diapositiva_blanca(d, f, W, H):
    d.rect(f, 'diapo/fondo', 0, 0, W, H, [solido('#FFFFFF')])
    d.texto(f, 'Resultados del trimestre', 96, 120, 54, '#1F2733', nombre='diapo/titulo')
    d.rect(f, 'diapo/raya', 96, 200, 160, 6, [solido('#2F6FDE')])
    for i, (v, c) in enumerate(((0.45, '#9DB8EE'), (0.62, '#6E97E6'), (0.8, '#2F6FDE'), (0.93, '#1D4FB0'))):
        d.rect(f, f'diapo/barra{i}', 140 + i * 190, 820 - 520 * v, 120, 520 * v, [solido(c)], radius=6)
    d.rect(f, 'diapo/eje', 110, 820, 800, 3, [solido('#C9CED6')])
    for i, t in enumerate(('Ingresos +18 %', 'Clientes 1 240', 'Retención 91 %')):
        d.texto(f, t, 1020, 330 + i * 120, 40, '#1F2733', est='Regular', nombre='diapo/dato')
    d.texto(f, 'Datos de muestra de la diapositiva, no del producto', 96, H - 56, 18, '#8B93A1', est='Regular')

def foto_oscura(d, f, W, H):
    d.rect(f, 'foto/cielo', 0, 0, W, H, [grad(['#2B3A55', '#6B4E5E', '#D08A55', '#3A2A22'])])
    d.rect(f, 'foto/sol', W * 0.62, H * 0.42, 260, 260, [{'type': 'radialGradient', 'stops': [
        {'color': '#FFE3A8', 'position': 0}, {'color': '#FFB86B00', 'position': 1}]}], tipo='ellipse')
    d.vector(f, 'foto/montes', f'M0 {H*0.72} L{W*0.18} {H*0.55} L{W*0.36} {H*0.7} L{W*0.55} {H*0.5} L{W*0.78} {H*0.68} '
             f'L{W} {H*0.58} L{W} {H} L0 {H} Z', 0, H * 0.5, W, H * 0.5, fill='#1B1A1F')

# ── pantallas ──
def pantalla_lectura(d, x, y, nombre, *, modo, estado, actual, texto='marfil', fondo=(LECT_FONDO, 1.0),
                     banda=True, halo=False, fondo_dibujo=None, chips=(), mic=None, barra_op=0.94, marca=ORO):
    W, H = 1600, 900
    f = d.frame(nombre, x, y, W, H, [solido(LECT_FONDO)])
    if fondo_dibujo: fondo_dibujo(d, f, W, H)
    sec = seccion_de(actual)
    lectura(d, f, 0, 80, W, H - 80, size=72, lh=104, margen=128, actual=actual, texto=texto, banda=banda,
            halo=halo, fondo=fondo, foco=0.33, marca=marca)
    barra(d, f, W, modo=modo, estado=estado, actual=actual, seccion=sec, chips=chips, mic=mic, op=barra_op)
    return f

def seccion_de(actual):
    gi, sec = 0, 'Inicio'
    for b in guion():
        if b[0] == 'dir':
            if gi <= actual: sec = b[1]
        else:
            gi += len(b[1])
    return sec

def pantalla_cuenta(d, x, y):
    f = pantalla_lectura(d, x, y, 'lectura/cuenta-atras', modo='manual', estado='cuenta', actual=0)
    d.rect(f, 'cuenta-atras', 0, 80, 1600, 820, [solido(OB_BAJO, 0.78)])
    cx, cy, r = 800, 470, 150
    d.elipse(f, 'cuenta/aro', cx - r, cy - r, 2 * r, 2 * r, [], stroke='#2A2B30', sw=10)
    # dos tercios del aro en oro (faltan 2 de 3 s); el arco se dibuja como trazo
    import math
    a0, a1 = -math.pi / 2, -math.pi / 2 + 2 * math.pi * 2 / 3
    p = f'M{cx + r * math.cos(a0):.2f} {cy + r * math.sin(a0):.2f} A{r} {r} 0 1 1 {cx + r * math.cos(a1):.2f} {cy + r * math.sin(a1):.2f}'
    d.vector(f, 'cuenta/arco', p, None, None, None, None, stroke=ORO, sw=10)
    d.steps[-1]['params'] = {k: v for k, v in d.steps[-1]['params'].items() if v is not None}
    cb, _ = caja(200)
    d.texto(f, '3', cx - 200, cy - cb / 2, 200, ORO_PALIDO, w=400, alin='center', nombre='cuenta-num')
    d.texto(f, 'Empieza en', cx - 300, cy - r - 80, 26, UI_1, w=600, alin='center')
    d.texto(f, 'Esc cancela · Espacio empieza ya', cx - 400, cy + r + 40, 22, UI_2, est='Regular', w=800,
            alin='center')
    return f

def aviso(d, padre, x, y, w, tema, nivel, titulo, msg, accion=None, size_t=20, size_m=18, compacta=False):
    color = {'info': ORO, 'aviso': AVISO, 'error': ERROR}[nivel]
    glifo = {'conexion': G_ENCHUFE, 'micro': G_MIC, 'modelo': G_MODELO}[tema]
    cbt, _ = caja(size_t); cbm, _ = caja(size_m, est='Regular')
    tx = x + 64
    aw = (ancho(accion, 17) + 32) if accion else 0
    tw = w - 64 - 20 - ((aw + 16) if accion and not compacta else 0)
    lineas = partir(msg, size_m, tw, est='Regular')
    h = 18 + cbt + 4 + len(lineas) * cbm + 18 + ((cbm + 22) if accion and compacta else 0)
    d.rect(padre, f'notice/{tema}', x, y, w, h, [solido(PANEL, 0.97)], radius=12, stroke=BORDE,
           effects=[sombra('#00000099', 24, 8)])
    d.rect(padre, 'notice/acento', x, y, 5, h, [solido(color)], radius=2)
    d.rect(padre, 'notice/icono-fondo', x + 18, y + 18, 34, 34, [solido(color, 0.14)], radius=17)
    d.vector(padre, f'glifo/{tema}', glifo, x + 26, y + 26, 18, 18, stroke=color, sw=1.8)
    d.texto(padre, titulo, tx, y + 16, size_t, UI_1, nombre='notice-titulo')
    for i, l in enumerate(lineas):
        d.texto(padre, l, tx, y + 16 + cbt + 4 + i * cbm, size_m, UI_2, est='Regular', nombre='notice-msg')
    if accion:
        bh = 34
        if compacta: bx, by = tx, y + h - 18 - bh
        else: bx, by = x + w - 20 - aw, y + (h - bh) / 2
        d.rect(padre, 'notice-action', bx, by, aw, bh, [], radius=8, stroke=color)
        d.texto(padre, accion, bx, by + (bh - caja(17)[0]) / 2, 17, color, w=aw, alin='center')
    return h

def pantalla_avisos(d, x, y):
    f = pantalla_lectura(d, x, y, 'avisos', modo='voz', estado='pausa', actual=31,
                         chips=(('Servidor', 'error'), ('Micro', 'aviso'), ('Modelo', 'aviso')))
    w, ax, ay = 820, 390, 104
    for tema, nivel, t, m, a in (
            ('conexion', 'error', 'Sin conexión con la app',
             'Reintento cada 2 s. El texto sigue en pantalla y puedes avanzar a mano.', 'Reintentar'),
            ('micro', 'aviso', 'El micrófono no da señal',
             'Asistido y Voz quedan en pausa. Revisa el micrófono en Windows.', 'Pasar a Manual'),
            ('modelo', 'info', 'Cargando el modelo de voz',
             'Manual ya funciona. Voz estará lista en cuanto termine la carga.', None)):
        ay += aviso(d, f, ax, ay, w, tema, nivel, t, m, a, size_t=22, size_m=19) + 12
    return f

# ── configuración ──
def deslizador(d, p, x, y, w, etiqueta, valor, frac, size=18):
    cb, _ = caja(size)
    d.texto(p, etiqueta, x, y, size, UI_1, est='Regular', nombre=f'campo/{etiqueta}')
    d.texto(p, valor, x, y, size, ORO_CLARO, fam='Consolas', est='Regular', w=w, alin='right', nombre='campo/valor')
    ry = y + cb + 14
    d.rect(p, 'riel', x, ry, w, 6, [solido('#25262B')], radius=3)
    d.rect(p, 'riel/lleno', x, ry, w * frac, 6, [grad(GRAD_ORO, (0, 0), (1, 0))], radius=3)
    d.elipse(p, 'riel/pomo', x + w * frac - 11, ry - 8, 22, 22, [solido(ORO_PALIDO)], stroke=OB_BAJO, sw=2)
    return cb + 14 + 6 + 30

def interruptor(d, p, x, y, w, etiqueta, on, nota=None, size=18):
    cb, _ = caja(size)
    d.texto(p, etiqueta, x, y, size, UI_1, est='Regular', nombre=f'campo/{etiqueta}')
    tx = x + w - 52
    d.rect(p, 'interruptor', tx, y + (cb - 28) / 2, 52, 28, [grad(GRAD_ORO, (0, 0), (1, 0))] if on else [solido('#25262B')],
           radius=14, stroke=None if on else BORDE)
    d.elipse(p, 'interruptor/pomo', tx + (27 if on else 3), y + (cb - 28) / 2 + 3, 22, 22,
             [solido(OB_BAJO if on else UI_2)])
    h = cb
    if nota:
        cbn, _ = caja(15, est='Regular')
        d.texto(p, nota, x, y + cb + 2, 15, UI_3, est='Regular'); h += cbn + 2
    return h + 22

def titulo_sec(d, p, x, y, s, w):
    d.texto(p, s, x, y, 15, ORO, ls=2.5, mayus=True, nombre='panel-titulo')
    d.rect(p, 'panel-titulo/raya', x, y + 30, w, 1, [solido(BORDE)])
    return 48

def boton(d, p, x, y, s, primario=False, size=18, h=46, pad=24):
    w = ancho(s, size) + 2 * pad
    if primario: d.rect(p, 'btn-primario', x, y, w, h, [grad(GRAD_ORO)], radius=10)
    else: d.rect(p, 'btn-secundario', x, y, w, h, [solido(CAMPO)], radius=10, stroke=BORDE)
    d.texto(p, s, x, y + (h - caja(size)[0]) / 2, size, '#141414' if primario else UI_1, w=w, alin='center')
    return w

def muestras(d, p, x, y, activo, size=16):
    for i, (n, c) in enumerate(TEXTO.items()):
        bx = x + i * 112
        if n == activo: d.rect(p, 'muestra/aro', bx - 4, y - 4, 52, 52, [], radius=12, stroke=ORO, sw=2)
        d.rect(p, f'muestra-color/{n}', bx, y, 44, 44, [solido(LECT_FONDO)], radius=8, stroke=BORDE)
        d.texto(p, 'Aa', bx, y + (44 - caja(20)[0]) / 2, 20, c, w=44, alin='center')
        d.texto(p, n.capitalize(), bx + 52, y + (44 - caja(size)[0]) / 2, size, UI_1 if n == activo else UI_2,
                est='Regular')
    return 44 + 26

def segmentado_campo(d, p, x, y, activo, size=18):
    return segmentado(d, p, x, y, 44, size, activo, pad=20) and 44 + 26

def cabecera(d, f, W, compacta=False):
    H = 64 if compacta else 80
    d.rect(f, 'cabecera', 0, 0, W, H, [grad([OB_ALTO, OB_BAJO])])
    d.rect(f, 'cabecera/raya', 0, H - 1, W, 1, [solido(ORO, 0.35)])
    s = 22 if compacta else 26
    cb, _ = caja(s)
    _, w = d.texto(f, 'Teleprompter', 20 if compacta else 32, (H - cb) / 2, s, ORO_PALIDO)
    if not compacta:
        d.texto(f, '100 % local · sin red', 32 + w + 16, (H - caja(15, 'Consolas', 'Regular')[0]) / 2, 15, UI_3,
                fam='Consolas', est='Regular')
    return H

def pantalla_config(d, x, y):
    W, H = 1600, 900
    f = d.frame('configuracion', x, y, W, H, [grad(['#15161A', OB_BAJO])])
    hh = cabecera(d, f, W)
    bw = ancho('Empezar a leer', 18) + 48
    boton(d, f, W - 32 - bw, (hh - 46) / 2, 'Empezar a leer', primario=True)
    bw2 = ancho('Pantalla completa', 18) + 48
    boton(d, f, W - 32 - bw - 16 - bw2, (hh - 46) / 2, 'Pantalla completa')
    cols = [(32, 560), (632, 452), (1116, 452)]
    top = hh + 32
    # columna 1: guion
    cx, cw = cols[0]
    y1 = top + titulo_sec(d, f, cx, top, 'Guion', cw)
    d.texto(f, 'Abrir de guiones/', cx, y1, 18, UI_1, est='Regular')
    y1 += 32
    d.rect(f, 'select-guion', cx, y1, cw, 46, [solido(CAMPO)], radius=10, stroke=BORDE)
    d.texto(f, 'ejemplo.txt', cx + 16, y1 + (46 - caja(18)[0]) / 2, 18, UI_1, est='Regular')
    d.vector(f, 'glifo/flecha', 'M1 1 L7 7 L13 1', cx + cw - 30, y1 + 19, 12, 7, stroke=ORO, sw=2)
    y1 += 46 + 22
    d.texto(f, 'O pégalo aquí', cx, y1, 18, UI_1, est='Regular'); y1 += 32
    ta_h = 420
    d.rect(f, 'textarea', cx, y1, cw, ta_h, [solido(CAMPO)], radius=10, stroke=BORDE)
    ty = y1 + 16
    for b in guion():
        if b[0] == 'dir':
            linea = [f'[{b[1]}]']
        else:
            linea = partir(' '.join(b[1]), 17, cw - 32, est='Regular')
        for l in linea:
            if ty > y1 + ta_h - 34: break
            d.texto(f, l, cx + 16, ty, 17, ORO_CLARO if l.startswith('[') else UI_2, est='Regular')
            ty += 25
        ty += 10
    y1 += ta_h + 18
    d.texto(f, 'Una línea en blanco separa párrafos. [Corchetes]: secciones, no se leen.', cx, y1, 15, UI_3,
            est='Regular')
    boton(d, f, cx, y1 + 34, 'Cargar guion', primario=False)
    # columna 2: lectura y fondo
    cx, cw = cols[1]
    y2 = top + titulo_sec(d, f, cx, top, 'Lectura', cw)
    y2 += deslizador(d, f, cx, y2, cw, 'Tamaño de letra', '1,0×', 0.4)
    y2 += deslizador(d, f, cx, y2, cw, 'Interlineado', '1,45', 0.3)
    d.texto(f, 'Color del texto', cx, y2, 18, UI_1, est='Regular'); y2 += 36
    y2 += muestras(d, f, cx, y2, 'marfil') + 8
    y2 += titulo_sec(d, f, cx, y2, 'Fondo', cw)
    y2 += deslizador(d, f, cx, y2, cw, 'Fondo', '100 %', 1.0)
    y2 += interruptor(d, f, cx, y2, cw, 'Banda de lectura', True, 'Franja oscura detrás de las líneas actuales')
    y2 += interruptor(d, f, cx, y2, cw, 'Halo', False, 'Contorno oscuro en cada letra')
    d.rect(f, 'preajuste', cx, y2, cw, 64, [solido(ORO, 0.08)], radius=12, stroke=ORO)
    d.texto(f, 'Sobre presentación', cx + 18, y2 + 8, 18, ORO_CLARO)
    d.texto(f, 'Banda + halo + fondo al 0 %', cx + 18, y2 + 34, 15, UI_2, est='Regular')
    # columna 3: avance y ventana
    cx, cw = cols[2]
    y3 = top + titulo_sec(d, f, cx, top, 'Avance', cw)
    y3 += segmentado_campo(d, f, cx, y3, 'voz')
    y3 += deslizador(d, f, cx, y3, cw, 'Velocidad', f'{VEL} · {int(PPS * 60)} ppm', (VEL - 1) / 19)
    y3 += deslizador(d, f, cx, y3, cw, 'Asistido: pausa tras silencio', '1,5 s', 0.3)
    d.texto(f, 'Frenos de Voz', cx, y3, 16, UI_2, ls=1.5, mayus=True); y3 += 34
    y3 += deslizador(d, f, cx, y3, cw, 'Confirmar con', '3 palabras', 0.4)
    y3 += deslizador(d, f, cx, y3, cw, 'Adelanto máximo', '6 palabras', 0.5)
    y3 += deslizador(d, f, cx, y3, cw, 'Velocidad máxima', '4 pal/s', 0.4)
    y3 += titulo_sec(d, f, cx, y3, 'Ventana', cw)
    for etq, on in (('Espejo', False), ('Siempre adelante', True), ('Invisible en capturas', True)):
        y3 += interruptor(d, f, cx, y3, cw, etq, on) - 6
    # atajos
    d.rect(f, 'atajos', 0, H - 56, W, 56, [solido(OB_BAJO)])
    ax = 32
    for k, s in (('Espacio', 'leer o pausar'), ('M', 'modo'), ('↑ ↓', 'velocidad'), ('← →', 'mover'),
                 ('R', 'inicio'), ('Esc', 'configuración'), ('F11', 'pantalla completa')):
        kw = ancho(k, 15, 'Consolas', 'Regular') + 16
        d.rect(f, 'kbd', ax, H - 42, kw, 28, [solido(CAMPO)], radius=6, stroke=BORDE)
        d.texto(f, k, ax, H - 42 + (28 - caja(15, 'Consolas', 'Regular')[0]) / 2, 15, UI_1, fam='Consolas',
                est='Regular', w=kw, alin='center')
        _, w = d.texto(f, s, ax + kw + 8, H - 42 + (28 - caja(15)[0]) / 2, 15, UI_2, est='Regular')
        ax += kw + 8 + w + 26
    return f

# ── versión compacta (400 px) ──
def barra_compacta(d, p, W, *, modo, estado, actual):
    H = 92
    d.rect(p, 'barra/fondo', 0, 0, W, H, [grad([OB_ALTO, OB_BAJO])])
    segmentado(d, p, 12, 10, 36, 15, modo, pad=11)
    hecho, falta, prog = tiempos(actual)
    cbm, _ = caja(18, 'Consolas', 'Regular')
    d.texto(p, falta, 0, 10 + (36 - cbm) / 2, 18, UI_1, fam='Consolas', est='Regular', w=W - 12, alin='right',
            nombre='barra-tiempo')
    g = 14
    gx = W - 12 - ancho(falta, 18, 'Consolas', 'Regular') - 12 - g
    if estado == 'pausa':
        d.vector(p, 'glifo/pausa', G_PAUSA, gx, 10 + (36 - g) / 2, g * 0.85, g, fill=ORO_CLARO)
    else:
        d.vector(p, 'glifo/play', G_PLAY, gx, 10 + (36 - g) / 2, g * 0.85, g, fill=ORO)
    cb13, _ = caja(13)
    d.texto(p, seccion_de(actual), 12, 56 + (26 - cb13) / 2, 13, ORO_CLARO, ls=1.5, mayus=True, nombre='barra-seccion')
    s = f'vel {VEL} · {int(PPS * 60)} ppm'
    d.texto(p, s, 0, 56 + (26 - caja(14, 'Consolas', 'Regular')[0]) / 2, 14, UI_2, fam='Consolas', est='Regular',
            w=W - 12, alin='right', nombre='barra-velocidad')
    d.rect(p, 'barra-progreso', 0, H, W, 3, [solido('#24252A')])
    d.rect(p, 'barra-progreso-relleno', 0, H, round(W * prog), 3, [grad(GRAD_ORO, (0, 0), (1, 0))])
    return H + 3

def pantalla_compacta_lectura(d, x, y, nombre='compacta/lectura', estado='corriendo', avisos=False):
    W, H = 400, 700
    f = d.frame(nombre, x, y, W, H, [solido(LECT_FONDO)])
    actual = 31
    lectura(d, f, 0, 95, W, H - 95, size=34, lh=48, margen=20, actual=actual, texto='marfil', foco=0.36)
    barra_compacta(d, f, W, modo='manual', estado=estado, actual=actual)
    if avisos:
        aviso(d, f, 12, 108, W - 24, 'micro', 'aviso', 'El micrófono no da señal',
              'Asistido y Voz quedan en pausa. Revisa el micrófono en Windows.', 'Pasar a Manual', size_t=18,
              size_m=16, compacta=True)
    return f

def pantalla_compacta_config(d, x, y):
    W, H = 400, 960
    f = d.frame('compacta/configuracion', x, y, W, H, [grad(['#15161A', OB_BAJO])])
    hh = cabecera(d, f, W, compacta=True)
    bw = ancho('Leer', 16) + 36
    boton(d, f, W - 16 - bw, (hh - 38) / 2, 'Leer', primario=True, size=16, h=38, pad=18)
    cx, cw, yy = 16, W - 32, hh + 20
    d.rect(f, 'select-guion', cx, yy, cw, 44, [solido(CAMPO)], radius=10, stroke=BORDE)
    d.texto(f, 'ejemplo.txt', cx + 14, yy + (44 - caja(17)[0]) / 2, 17, UI_1, est='Regular')
    d.vector(f, 'glifo/flecha', 'M1 1 L7 7 L13 1', cx + cw - 28, yy + 18, 12, 7, stroke=ORO, sw=2)
    yy += 44 + 24
    yy += titulo_sec(d, f, cx, yy, 'Avance', cw)
    segmentado(d, f, cx, yy, 40, 16, 'manual', pad=16); yy += 40 + 22
    yy += deslizador(d, f, cx, yy, cw, 'Velocidad', f'{VEL} · {int(PPS * 60)} ppm', (VEL - 1) / 19, size=17)
    yy += titulo_sec(d, f, cx, yy, 'Lectura', cw)
    yy += deslizador(d, f, cx, yy, cw, 'Tamaño de letra', '1,0×', 0.4, size=17)
    for i, (n, c) in enumerate(TEXTO.items()):
        bx = cx + i * 120
        if n == 'marfil': d.rect(f, 'muestra/aro', bx - 3, yy - 3, 44, 44, [], radius=10, stroke=ORO, sw=2)
        d.rect(f, f'muestra-color/{n}', bx, yy, 38, 38, [solido(LECT_FONDO)], radius=8, stroke=BORDE)
        d.texto(f, 'Aa', bx, yy + (38 - caja(17)[0]) / 2, 17, c, w=38, alin='center')
        d.texto(f, n.capitalize(), bx + 46, yy + (38 - caja(15)[0]) / 2, 15, UI_1 if n == 'marfil' else UI_2,
                est='Regular')
    yy += 38 + 26
    yy += titulo_sec(d, f, cx, yy, 'Fondo', cw)
    yy += deslizador(d, f, cx, yy, cw, 'Fondo', '100 %', 1.0, size=17)
    yy += interruptor(d, f, cx, yy, cw, 'Banda de lectura', True, size=17)
    yy += interruptor(d, f, cx, yy, cw, 'Halo', False, size=17)
    d.rect(f, 'preajuste', cx, yy, cw, 60, [solido(ORO, 0.08)], radius=12, stroke=ORO)
    d.texto(f, 'Sobre presentación', cx + 16, yy + 7, 17, ORO_CLARO)
    d.texto(f, 'Banda + halo + fondo al 0 %', cx + 16, yy + 32, 14, UI_2, est='Regular')
    yy += 60 + 20
    for s in ('Frenos de Voz', 'Ventana y atajos'):
        d.rect(f, 'plegado', cx, yy, cw, 48, [solido(CAMPO)], radius=10, stroke=BORDE)
        d.texto(f, s, cx + 14, yy + (48 - caja(17)[0]) / 2, 17, UI_1)
        d.vector(f, 'glifo/flecha', 'M1 1 L7 7 L13 1', cx + cw - 28, yy + 20, 12, 7, stroke=ORO, sw=2)
        yy += 48 + 10
    return f

# ── hoja de tokens ──
def pantalla_tokens(d, x, y):
    W, H = 1600, 900
    f = d.frame('tokens', x, y, W, H, [solido(OB_BAJO)])
    d.texto(f, 'Tema v3 · obsidiana y oro', 40, 32, 30, ORO_PALIDO)
    d.texto(f, 'Los mismos valores que la colección «Galleon» de los Stream Deck. La lectura es neutra.', 40, 78, 18,
            UI_2, est='Regular')
    sw = [('--tp-obsidiana-alto', OB_ALTO), ('--tp-obsidiana-bajo', OB_BAJO), ('--tp-lectura-fondo', LECT_FONDO),
          ('--tp-oro', ORO), ('--tp-oro-claro', ORO_CLARO), ('--tp-oro-palido', ORO_PALIDO),
          ('--tp-oro-oscuro', ORO_OSCURO), ('--tp-texto-blanco', TEXTO['blanco']), ('--tp-texto-marfil', TEXTO['marfil']),
          ('--tp-lectura-leida', LEIDA), ('--tp-ok', OK), ('--tp-aviso', AVISO), ('--tp-error', ERROR),
          ('--tp-grafito', GRAFITO)]
    for i, (n, c) in enumerate(sw):
        cx, cy = 40 + (i % 7) * 222, 130 + (i // 7) * 150
        d.rect(f, f'muestra/{n}', cx, cy, 200, 80, [solido(c)], radius=10, stroke=BORDE)
        d.texto(f, n, cx, cy + 88, 15, UI_1, fam='Consolas', est='Regular')
        d.texto(f, c, cx, cy + 110, 15, UI_3, fam='Consolas', est='Regular')
    y0 = 450
    d.texto(f, 'Lectura · Segoe UI Semibold', 40, y0, 15, ORO, ls=2.5, mayus=True)
    d.texto(f, 'Hoy vamos a presentar', 40, y0 + 34, 72, TEXTO['marfil'])
    d.texto(f, 'leído', 40, y0 + 140, 40, LEIDA)
    _, w = d.texto(f, 'actual', 180, y0 + 140, 40, TEXTO['marfil'])
    d.rect(f, 'marca-actual', 178, y0 + 140 + caja(40)[1] + 5, w + 4, 4, [solido(ORO)], radius=2)
    d.texto(f, 'por leer', 340, y0 + 140, 40, TEXTO['marfil'])
    d.texto(f, 'Interfaz · Segoe UI · cifras en Consolas', 40, y0 + 230, 15, ORO, ls=2.5, mayus=True)
    d.texto(f, 'Modo, estado y tiempo: 20–24 px, se leen a 2 m', 40, y0 + 262, 22, UI_1, est='Regular')
    d.texto(f, '0:13  −0:17   vel 6 · 180 ppm', 40, y0 + 300, 24, ORO_CLARO, fam='Consolas', est='Regular')
    # componentes
    cx = 860
    d.texto(f, 'Componentes', cx, y0, 15, ORO, ls=2.5, mayus=True)
    segmentado(d, f, cx, y0 + 34, 44, 20, 'asistido')
    chip(d, f, cx, y0 + 100, 'Servidor', 'ok'); chip(d, f, cx + 130, y0 + 100, 'Micro', 'aviso')
    chip(d, f, cx + 240, y0 + 100, 'Modelo', 'error')
    boton(d, f, cx, y0 + 150, 'Empezar a leer', primario=True); boton(d, f, cx + 210, y0 + 150, 'Cargar guion')
    interruptor(d, f, cx, y0 + 220, 300, 'Banda de lectura', True)
    interruptor(d, f, cx, y0 + 262, 300, 'Halo', False)
    d.rect(f, 'halo/muestra', cx + 360, y0 + 210, 340, 110, [solido('#FFFFFF')], radius=10)
    d.texto(f, 'Halo sobre blanco', cx + 380, y0 + 240, 34, '#FFFFFF', halo=True)
    return f

# ── documento ──
def documento():
    d = Doc()
    for (fam, est), arch in ARCH.items():
        d.step('text.loadFont', {'path': os.path.join(FUENTES, arch)})
    G = 120
    pantallas = []
    y = 0
    pantallas.append(pantalla_lectura(d, 0, y, 'lectura/manual', modo='manual', estado='corriendo', actual=31,
                                      chips=(('Servidor', 'ok'),)))
    pantallas.append(pantalla_lectura(d, 1600 + G, y, 'lectura/pausa', modo='manual', estado='pausa', actual=31,
                                      texto='oro', marca=TEXTO['blanco'], chips=(('Servidor', 'ok'),)))
    y += 900 + G
    pantallas.append(pantalla_lectura(d, 0, y, 'lectura/asistido-sobre-presentacion', modo='asistido',
                                      estado='espera', actual=31, texto='blanco', fondo=None, halo=True,
                                      fondo_dibujo=diapositiva_blanca, barra_op=0.9, mic=0))
    pantallas.append(pantalla_lectura(d, 1600 + G, y, 'lectura/voz', modo='voz', estado='corriendo', actual=31,
                                      texto='marfil', fondo=(LECT_FONDO, 0.45), halo=True, fondo_dibujo=foto_oscura,
                                      barra_op=0.9, mic=4, chips=(('Siguiendo', 'ok'),)))
    y += 900 + G
    pantallas.append(pantalla_cuenta(d, 0, y))
    pantallas.append(pantalla_avisos(d, 1600 + G, y))
    y += 900 + G
    pantallas.append(pantalla_config(d, 0, y))
    pantallas.append(pantalla_tokens(d, 1600 + G, y))
    y += 900 + G
    pantallas.append(pantalla_compacta_lectura(d, 0, y))
    pantallas.append(pantalla_compacta_lectura(d, 400 + 60, y, 'compacta/avisos', estado='pausa', avisos=True))
    pantallas.append(pantalla_compacta_config(d, 920, y))
    return d, pantallas

# ── construir ──
def sha(p):
    with open(p, 'rb') as fh: return hashlib.sha256(fh.read()).hexdigest()

def nombre_png(d, ref):
    s = next(s for s in d.steps if s.get('as') == ref)
    return s['params']['name'].replace('/', '__') + '.png'

def correr(d, pantallas, out):
    pasos = list(d.steps)
    for ref in pantallas:
        pasos.append({'command': 'file.export', 'params': {'ids': [{'$ref': ref}], 'format': 'png', 'scale': 1,
                                                            'path': os.path.join(out, 'png', nombre_png(d, ref))}})
    os.makedirs(os.path.join(out, 'png'), exist_ok=True)
    guion_p = os.path.join(out, 'guion.json')
    with open(guion_p, 'w', encoding='utf-8', newline='\n') as fh: json.dump(pasos, fh, ensure_ascii=False)
    fc = os.path.join(out, 'diseno_v3.figcraft')
    args = [CLI, '--no-system-fonts', 'run', guion_p, '--out', fc, '--automation-read-root', out,
            '--automation-read-root', FUENTES, '--automation-write-root', out]
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8')
    malos = [l for l in r.stdout.splitlines() if '"ok":false' in l]
    if r.returncode != 0 or malos:
        sys.exit(f'figcraft-cli falló (exit {r.returncode}): {" | ".join(malos[:3])} {r.stderr[-600:]}')
    return fc

def main():
    d, pantallas = documento()
    with open(os.path.join(HERE, 'diseno_v3.json'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('[\n' + ',\n'.join(json.dumps(s, ensure_ascii=False) for s in d.steps) + '\n]\n')
    corridas = []
    for k in (1, 2):
        out = tempfile.mkdtemp(prefix=f'diseno_v3_{k}_')
        fc = correr(d, pantallas, out)
        hs = {'diseno_v3.figcraft': sha(fc)}
        for n in sorted(os.listdir(os.path.join(out, 'png'))):
            hs['png/' + n] = sha(os.path.join(out, 'png', n))
        corridas.append((out, hs))
    (o1, h1), (_, h2) = corridas
    distintos = [k for k in h1 if h1[k] != h2.get(k)]
    if distintos or h1.keys() != h2.keys():
        sys.exit(f'no determinista: {distintos}')
    os.makedirs(os.path.join(HERE, 'png'), exist_ok=True)
    shutil.copyfile(os.path.join(o1, 'diseno_v3.figcraft'), os.path.join(HERE, 'diseno_v3.figcraft'))
    for k in h1:
        if k.startswith('png/'): shutil.copyfile(os.path.join(o1, k), os.path.join(HERE, k))
    with open(os.path.join(HERE, 'SHA256SUMS.txt'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.writelines(f'{v}  {k}\n' for k, v in sorted(h1.items()))
    print(f'{len(d.steps)} comandos · {len(h1)} archivos con el mismo SHA-256 en las dos corridas')
    print(f'diseno_v3.figcraft {h1["diseno_v3.figcraft"][:16]}')

if __name__ == '__main__':
    main()
