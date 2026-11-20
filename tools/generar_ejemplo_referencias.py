"""Hito 85 (R6): genera los recursos de `guiones/ejemplo_referencias.txt`, sin red.

    .venv312\\Scripts\\python.exe tools\\generar_ejemplo_referencias.py

Escribe en `guiones/ejemplo_referencias_recursos/`:
- portada.png: una imagen (Pillow);
- informe.pdf: tres páginas (Pillow);
- presentacion.pptx: dos diapositivas, convertidas con LibreOffice desde un ODP
  plano escrito aquí (si no hay LibreOffice, se avisa y no se genera);
- app.py: un trozo de código.
No sobrescribe un archivo que ya exista (nada se borra): para rehacer uno,
apártalo antes a `_anterior/`.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import slides_engine  # noqa: E402

OUT = ROOT / "guiones" / "ejemplo_referencias_recursos"
OBS, ORO, ORO_CLARO, MARFIL, GRAFITO = "#0B0C0E", "#D9B04C", "#F2D88C", "#F3EEE2", "#8C8778"

APP_PY = '''"""Ejemplo de código para el guion de referencias."""
from dataclasses import dataclass


@dataclass
class Venta:
    mes: str
    total: float


def crecimiento(ventas: list[Venta]) -> float:
    """Crecimiento porcentual entre el primer y el último mes."""
    if len(ventas) < 2 or ventas[0].total == 0:
        return 0.0
    return (ventas[-1].total / ventas[0].total - 1) * 100


if __name__ == "__main__":
    datos = [Venta("enero", 120.0), Venta("febrero", 135.5), Venta("marzo", 162.0)]
    print(f"Crecimiento del trimestre: {crecimiento(datos):.1f} %")
'''

FODP = '''<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
 xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.presentation">
 <office:automatic-styles>
  <style:page-layout style:name="PM1"><style:page-layout-properties fo:margin-top="0cm" fo:margin-bottom="0cm" fo:margin-left="0cm" fo:margin-right="0cm" fo:page-width="28cm" fo:page-height="15.75cm" style:print-orientation="landscape"/></style:page-layout>
  <style:style style:name="dp1" style:family="drawing-page"><style:drawing-page-properties draw:background-size="full" draw:fill="solid" draw:fill-color="#0B0C0E"/></style:style>
  <style:style style:name="gr1" style:family="graphic"><style:graphic-properties draw:stroke="none" draw:fill="none" draw:textarea-vertical-align="top"/></style:style>
  <style:style style:name="gr2" style:family="graphic"><style:graphic-properties draw:stroke="none" draw:fill="solid" draw:fill-color="#D9B04C"/></style:style>
  <style:style style:name="P1" style:family="paragraph"><style:text-properties fo:font-size="40pt" fo:color="#F2D88C" fo:font-weight="bold" style:font-name="Segoe UI"/></style:style>
  <style:style style:name="P2" style:family="paragraph"><style:text-properties fo:font-size="24pt" fo:color="#F3EEE2" style:font-name="Segoe UI"/></style:style>
 </office:automatic-styles>
 <office:master-styles>
  <style:master-page style:name="Default" style:page-layout-name="PM1" draw:style-name="dp1"/>
 </office:master-styles>
 <office:body><office:presentation>
  <draw:page draw:name="Portada" draw:master-page-name="Default" draw:style-name="dp1">
   <draw:rect draw:style-name="gr2" svg:x="2cm" svg:y="9.6cm" svg:width="8cm" svg:height="0.25cm"/>
   <draw:frame draw:style-name="gr1" svg:x="2cm" svg:y="5cm" svg:width="24cm" svg:height="4cm"><draw:text-box><text:p text:style-name="P1">Informe trimestral</text:p><text:p text:style-name="P2">Diapositiva 1 · portada</text:p></draw:text-box></draw:frame>
  </draw:page>
  <draw:page draw:name="Resultados" draw:master-page-name="Default" draw:style-name="dp1">
   <draw:frame draw:style-name="gr1" svg:x="2cm" svg:y="1.5cm" svg:width="24cm" svg:height="3cm"><draw:text-box><text:p text:style-name="P1">Tres resultados</text:p></draw:text-box></draw:frame>
   <draw:rect draw:style-name="gr2" svg:x="3cm" svg:y="9cm" svg:width="5cm" svg:height="4.5cm"/>
   <draw:rect draw:style-name="gr2" svg:x="11.5cm" svg:y="7cm" svg:width="5cm" svg:height="6.5cm"/>
   <draw:rect draw:style-name="gr2" svg:x="20cm" svg:y="5cm" svg:width="5cm" svg:height="8.5cm"/>
   <draw:frame draw:style-name="gr1" svg:x="2cm" svg:y="4.2cm" svg:width="24cm" svg:height="1.5cm"><draw:text-box><text:p text:style-name="P2">Diapositiva 2 · enero, febrero y marzo</text:p></draw:text-box></draw:frame>
  </draw:page>
 </office:presentation></office:body>
</office:document>
'''


def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def portada(path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1280, 720), OBS)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1279, 719), outline=ORO, width=6)
    d.rectangle((96, 470, 520, 482), fill=ORO)
    d.text((96, 250), "Proyecto Faro", font=_font(110), fill=ORO_CLARO)
    d.text((96, 400), "Portada · imagen de ejemplo", font=_font(44), fill=MARFIL)
    for i, h in enumerate((120, 200, 290)):
        d.rectangle((900 + i * 110, 600 - h, 980 + i * 110, 600), fill=ORO)
    img.save(path, format="PNG", optimize=True)


def informe(path: Path) -> None:
    from PIL import Image, ImageDraw

    pages = []
    for n, (titulo, sub) in enumerate((("Informe", "Página 1 · resumen"),
                                       ("Metodología", "Página 2 · cómo se midió"),
                                       ("Ventas del trimestre", "Página 3 · el gráfico")), start=1):
        pg = Image.new("RGB", (1240, 1754), "#FFFFFF")
        d = ImageDraw.Draw(pg)
        d.rectangle((0, 0, 1239, 160), fill=OBS)
        d.text((80, 40), titulo, font=_font(72), fill=ORO_CLARO)
        d.text((80, 220), sub, font=_font(44), fill="#222222")
        if n == 3:
            for i, (mes, h) in enumerate((("enero", 360), ("febrero", 420), ("marzo", 520))):
                x = 160 + i * 330
                d.rectangle((x, 1300 - h, x + 200, 1300), fill=ORO)
                d.text((x, 1330), mes, font=_font(40), fill="#222222")
        else:
            for k in range(8):
                d.rectangle((80, 360 + k * 90, 1160 - (k % 3) * 180, 380 + k * 90), fill="#D8D4C8")
        d.text((80, 1660), f"{n} / 3", font=_font(32), fill=GRAFITO)
        pages.append(pg)
    pages[0].save(path, format="PDF", save_all=True, append_images=pages[1:], resolution=150)


def presentacion(path: Path) -> str:
    soffice = slides_engine.find_soffice()
    if soffice is None:
        return "sin LibreOffice: presentacion.pptx no se generó"
    with tempfile.TemporaryDirectory(prefix="tp_fodp_") as tmp:
        src = Path(tmp) / "presentacion.fodp"
        src.write_text(FODP, encoding="utf-8")
        profile = (Path(tmp) / "perfil").as_uri()
        r = subprocess.run([str(soffice), "--headless", "--norestore", "--nologo", "--nodefault",
                            f"-env:UserInstallation={profile}", "--convert-to", "pptx",
                            "--outdir", str(path.parent), str(src)],
                           capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not path.is_file():
        return f"LibreOffice no generó presentacion.pptx (código {r.returncode}): {r.stderr.strip()[:200]}"
    return f"presentacion.pptx generada con {soffice}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = {"portada.png": portada, "informe.pdf": informe, "app.py": None, "presentacion.pptx": presentacion}
    for name, fn in jobs.items():
        path = OUT / name
        if path.exists():
            print(f"= {name}: ya existe, no se toca")
            continue
        if fn is None:
            path.write_text(APP_PY, encoding="utf-8", newline="\n")
            print(f"+ {name}")
        else:
            msg = fn(path)
            print(f"+ {name}" + (f": {msg}" if msg else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
