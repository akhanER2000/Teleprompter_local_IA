"""Hito 85 (R4): convierte una presentación a PDF con lo que haya instalado, sin red.

Primero PowerPoint (por automatización, con `cscript` y un VBScript: no hace
falta pywin32); si no está, LibreOffice sin ventana (`soffice --headless`).
Si no hay ninguno, `NoEngine`: la tarjeta pide exportar la diapositiva como
imagen. Las diapositivas se dibujan después desde el PDF (tools/ref_thumbs.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CONVERT_TIMEOUT_S = 120

_PPT_VBS = r'''Dim app, pres, started
On Error Resume Next
Set app = GetObject(, "PowerPoint.Application")
started = (Err.Number <> 0)
Err.Clear
On Error GoTo 0
If started Then Set app = CreateObject("PowerPoint.Application")
Set pres = app.Presentations.Open(WScript.Arguments(0), True, False, False)
pres.SaveAs WScript.Arguments(1), 32
pres.Close
If started And app.Presentations.Count = 0 Then app.Quit
'''


class NoEngine(RuntimeError):
    """No hay PowerPoint ni LibreOffice."""


class ConvertError(RuntimeError):
    """El motor existe pero no pudo convertir el archivo."""


def _hkcr(key: str) -> str | None:
    try:
        import winreg
    except ImportError:   # fuera de Windows
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key) as k:
            return winreg.QueryValue(k, "")
    except OSError:
        return None


def powerpoint_available() -> bool:
    """PowerPoint registrado para automatización (y cscript para lanzarlo)."""
    return _hkcr("PowerPoint.Application\\CLSID") is not None and shutil.which("cscript") is not None


def find_soffice() -> Path | None:
    """soffice.exe de LibreOffice: TP_SOFFICE, el registro COM, las carpetas de siempre o el PATH."""
    found: list[str] = []
    env = os.environ.get("TP_SOFFICE")
    if env:
        found.append(env)
    clsid = _hkcr("com.sun.star.ServiceManager\\CLSID")
    if clsid:
        server = _hkcr(f"CLSID\\{clsid}\\LocalServer32")
        if server:
            exe = server.split(" --", 1)[0].strip().strip('"')
            found.append(exe)
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            found.append(str(Path(base) / "LibreOffice" / "program" / "soffice.exe"))
    which = shutil.which("soffice")
    if which:
        found.append(which)
    for c in found:
        p = Path(c)
        if p.name.lower() == "soffice.bin":
            p = p.with_name("soffice.exe")
        if p.is_file():
            return p
    return None


def engine() -> str | None:
    """"powerpoint", "libreoffice" o None (lo que se usaría ahora)."""
    if powerpoint_available():
        return "powerpoint"
    if find_soffice() is not None:
        return "libreoffice"
    return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=CONVERT_TIMEOUT_S, creationflags=flags)
    except subprocess.TimeoutExpired as exc:
        raise ConvertError(f"la conversión tardó más de {CONVERT_TIMEOUT_S} s") from exc


def _with_powerpoint(src: Path, out_dir: Path) -> Path:
    out = out_dir / f"{src.stem}.pdf"
    with tempfile.TemporaryDirectory(prefix="tp_ppt_") as tmp:
        vbs = Path(tmp) / "exportar.vbs"
        vbs.write_text(_PPT_VBS, encoding="utf-8")
        r = _run(["cscript", "//nologo", "//B", str(vbs), str(src), str(out)])
    if r.returncode != 0 or not out.is_file():
        raise ConvertError(f"PowerPoint no exportó el PDF (código {r.returncode}): {r.stderr.strip()[:200]}")
    return out


def _with_libreoffice(src: Path, out_dir: Path, soffice: Path) -> Path:
    # Perfil propio: no choca con un LibreOffice abierto por el usuario.
    profile = (out_dir.parent / "lo_perfil").resolve().as_uri()
    r = _run([str(soffice), "--headless", "--norestore", "--nolockcheck", "--nologo", "--nodefault",
              f"-env:UserInstallation={profile}", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)])
    out = out_dir / f"{src.stem}.pdf"
    if r.returncode != 0 or not out.is_file():
        raise ConvertError(f"LibreOffice no convirtió el archivo (código {r.returncode}): "
                           f"{(r.stderr or r.stdout).strip()[:200]}")
    return out


def to_pdf(src: Path, out_dir: Path) -> tuple[Path, str]:
    """Convierte `src` a `out_dir/<stem>.pdf`. Devuelve (pdf, motor). Lanza NoEngine o ConvertError."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if powerpoint_available():
        return _with_powerpoint(src, out_dir), "powerpoint"
    soffice = find_soffice()
    if soffice is not None:
        return _with_libreoffice(src, out_dir, soffice), "libreoffice"
    raise NoEngine("no hay PowerPoint ni LibreOffice instalados")
