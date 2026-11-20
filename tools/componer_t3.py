"""Hito 84 (T3): compone las capturas transparentes de la lectura sobre tres diapositivas.

    C:\\Python314\\python.exe tools\\componer_t3.py docs\\hito84\\capturas

Necesita Pillow (está en el Python del sistema, no en .venv312). Lee t3_<color>_alfa.png
(de tools/capturas_v3.py) y escribe t3_<color>_<fondo>.png sobre: diapositiva negra,
diapositiva blanca y una foto (fondo de pantalla de Windows; si no hay, un degradado
con ruido). Imprime el contraste mínimo medido en la palabra actual.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

FOTOS = [Path(r"C:\Windows\Web\Wallpaper\ThemeA\img20.jpg"),
         Path(r"C:\Windows\Web\Wallpaper\Spotlight\img14.jpg")]


def diapositiva(color: str, size: tuple[int, int]) -> Image.Image:
    w, h = size
    fondo = (16, 16, 18) if color == "negra" else (250, 250, 248)
    tinta = (235, 235, 235) if color == "negra" else (30, 30, 34)
    im = Image.new("RGB", size, fondo)
    d = ImageDraw.Draw(im)
    # Una diapositiva típica: título y viñetas en bloques.
    d.rectangle([w * 0.08, h * 0.10, w * 0.62, h * 0.18], fill=tinta)
    for i in range(5):
        y = h * (0.30 + i * 0.11)
        d.ellipse([w * 0.08, y, w * 0.08 + 18, y + 18], fill=tinta)
        d.rectangle([w * 0.11, y + 2, w * (0.55 + 0.07 * (i % 3)), y + 16], fill=tinta)
    d.rectangle([w * 0.70, h * 0.30, w * 0.92, h * 0.80], outline=tinta, width=6)
    return im


def foto(size: tuple[int, int]) -> Image.Image:
    for p in FOTOS:
        if p.is_file():
            im = Image.open(p).convert("RGB")
            r = max(size[0] / im.width, size[1] / im.height)
            im = im.resize((round(im.width * r), round(im.height * r)), Image.LANCZOS)
            x, y = (im.width - size[0]) // 2, (im.height - size[1]) // 2
            return im.crop((x, y, x + size[0], y + size[1]))
    im = Image.effect_noise(size, 60).convert("RGB")
    return im.filter(ImageFilter.GaussianBlur(3))


def main(carpeta: Path) -> int:
    alfas = sorted(carpeta.glob("t3_*_alfa.png"))
    if not alfas:
        print("No hay t3_*_alfa.png: corre antes tools/capturas_v3.py")
        return 1
    for a in alfas:
        texto = Image.open(a).convert("RGBA")
        color = a.stem.split("_")[1]
        for nombre, base in (("negra", diapositiva("negra", texto.size)),
                             ("blanca", diapositiva("blanca", texto.size)),
                             ("foto", foto(texto.size))):
            out = base.convert("RGBA")
            out.alpha_composite(texto)
            out.convert("RGB").save(carpeta / f"t3_{color}_{nombre}.png", optimize=True)
            print(f"t3_{color}_{nombre}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
