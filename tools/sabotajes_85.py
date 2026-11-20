"""Hito 85: cada puerta nueva se ve FALLAR (exit 1) con un sabotaje real, y vuelve a verde.

    .venv312\\Scripts\\python.exe tools\\sabotajes_85.py

Igual que tools/sabotajes_84.py: cambia una línea, corre la prueba que la vigila, mira el
CÓDIGO DE SALIDA, restaura los bytes exactos y comprueba el SHA-256. Los sabotajes de la
caché van con la clave de caché rota a propósito (no hace falta subir THUMB_GEN: cada
prueba usa una carpeta de guiones nueva y vacía). Sale con 1 si algún sabotaje dio verde o
si algo no quedó como estaba.

Cada corrida usa su PROPIA carpeta de bytecode (PYTHONPYCACHEPREFIX): dos sabotajes del
mismo tamaño escritos en el mismo segundo reutilizaban el .pyc del anterior, y el sabotaje
de `pdf` dio verde sin haberse probado. Es el caché que miente, dentro de la comprobación.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PYTEST = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
REFS = "tests/test_referencias.py"
E2E = "tests/test_e2e_referencias.py"
TIPOS = "img|ppt|pdf|code|nota"

SABOTAJES = [
    # Un sabotaje por tipo: el prefijo sale de la expresión y [tipo: …] vuelve a ser directiva.
    *[(f"parser: [{t}: …] deja de ser referencia", "tools/script_parser.py",
       f"({TIPOS})" + r"\s*:",
       "(" + "|".join(x for x in TIPOS.split("|") if x != t) + r")\s*:",
       PYTEST + [REFS, "-k", f"parser_cada_tipo and {t}"]) for t in TIPOS.split("|")],
    ("archivo inexistente: la respuesta no dice «No encontrado»", "backend/refs.py",
     'return JSONResponse({"detail": f"No encontrado: {_name(file, extra)}"',
     'return JSONResponse({"detail": f"Error: {_name(file, extra)}"',
     PYTEST + [REFS, "-k", "inexistente"]),
    ("orden de búsqueda: guiones/ antes que _recursos/", "tools/ref_resolve.py",
     "        if res is not None:\n            out.append(res / rel)\n        out.append(scripts_dir / rel)",
     "        out.append(scripts_dir / rel)\n        if res is not None:\n            out.append(res / rel)",
     PYTEST + [REFS, "-k", "orden_de_busqueda"]),
    ("tarjeta rota sin «No encontrado» en el visor", "frontend/app.js",
     '    r.card.classList.add("ref-error");\n    r.body.textContent = text;',
     '    r.card.classList.add("ref-error");\n    r.body.textContent = "";  // SABOTAJE',
     PYTEST + [E2E]),
    ("la voz lee las referencias", "tools/script_parser.py",
     "            pos = end\n            continue",
     "            pos = start + 1  # SABOTAJE\n            continue",
     PYTEST + [REFS, "-k", "voz_las_ignora"]),
    ("orden view fuera del contrato", "backend/protocol.py",
     '"view": _ctl_view, "ref_zoom": _ctl_none,',
     '"ref_zoom": _ctl_none,  # SABOTAJE',
     PYTEST + [REFS, "-k", "orden_view"]),
    ("orden view no cambia la vista", "frontend/core.js",
     "        return { ...p, vista: next };",
     "        return { ...p };  // SABOTAJE",
     PYTEST + ["tests/test_frontend.py", E2E]),
    ("orden ref_zoom no se ejecuta en el visor", "frontend/app.js",
     'case "ref_zoom": toggleZoom(); break;',
     'case "ref_zoom": break;  // SABOTAJE',
     PYTEST + [E2E]),
    ("estado sin ref_actual", "frontend/app.js",
     "      ref_actual: nearRef() ? nearRef().seg.label || null : null,",
     "      ref_actual: null,  // SABOTAJE",
     PYTEST + [E2E]),
    ("caché que no ve el cambio del archivo", "tools/ref_thumbs.py",
     "st.st_mtime_ns, st.st_size, *parts",
     "0, 0, *parts",
     PYTEST + [REFS, "-k", "cache_se_rehace_si_cambia"]),
    ("caché sin versión", "tools/ref_thumbs.py",
     "(THUMB_GEN, src.resolve(),",
     "(src.resolve(),",
     PYTEST + [REFS, "-k", "cache_se_rehace_si_sube"]),
    ("Sobre presentación sin el 85 %", "frontend/core.js",
     "fondo_pct: 0, ref_opacidad: REF_OPACIDAD_PRESENTACION };",
     "fondo_pct: 0 };",
     PYTEST + ["tests/test_frontend.py"]),
    ("la tarjeta tapa el texto", "frontend/styles.css",
     "padding-right: calc(var(--tp-margen) + var(--tp-ref-ancho) + var(--tp-ref-hueco));",
     "padding-right: var(--tp-margen);",
     [PY, "tools/capturas_85.py", str(Path(tempfile.gettempdir()) / "tp_sabotaje_85")]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def env_limpio(n: int) -> dict:
    """Entorno con su propia carpeta de bytecode: ningún .pyc de otra corrida."""
    pref = Path(tempfile.gettempdir()) / f"tp_sabotajes_85_pyc_{os.getpid()}_{n}"
    return {**os.environ, "PYTHONPYCACHEPREFIX": str(pref)}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    malos = 0
    corrida = 0
    for nombre, rel, viejo, nuevo, cmd in SABOTAJES:
        path = ROOT / rel
        original = path.read_bytes()
        antes = sha(path)
        texto = original.decode("utf-8")
        crlf = "\r\n" in texto
        v, n = (viejo.replace("\n", "\r\n"), nuevo.replace("\n", "\r\n")) if crlf else (viejo, nuevo)
        if texto.count(v) != 1:
            print(f"✗ {nombre}: la línea a sabotear no está una sola vez en {rel}")
            malos += 1
            continue
        try:
            path.write_bytes(texto.replace(v, n).encode("utf-8"))
            corrida += 1
            code = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env_limpio(corrida),
                                  encoding="utf-8", errors="replace").returncode
        finally:
            path.write_bytes(original)
        restaurado = sha(path) == antes
        ok = code != 0 and restaurado
        malos += not ok
        print(f"{'✓' if ok else '✗'} {nombre}: exit {code} {'(rojo)' if code else '(VERDE: la puerta no ve el sabotaje)'}"
              f" · restaurado {'sí' if restaurado else 'NO'}")
    verde = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env_limpio(corrida + 1))
    print(f"{'✓' if verde.returncode == 0 else '✗'} vuelta a verde: exit {verde.returncode} · "
          f"{verde.stdout.strip().splitlines()[-1] if verde.stdout.strip() else ''}")
    malos += verde.returncode != 0
    return 1 if malos else 0


if __name__ == "__main__":
    sys.exit(main())
