"""Hito 84: cada puerta nueva se ve FALLAR (exit 1) con un sabotaje real, y vuelve a verde.

    .venv312\\Scripts\\python.exe tools\\sabotajes_84.py

Para cada sabotaje: cambia una línea del código, corre la prueba que lo vigila, mira el
CÓDIGO DE SALIDA, restaura los bytes exactos y comprueba el SHA-256. Sale con 1 si algún
sabotaje dio verde o si algo no quedó como estaba.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PYTEST = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]

SABOTAJES = [
    ("frenos: sin tope de adelanto", "tools/voice_brakes.py",
     "target = min(target, self._confirmed + p.max_ahead)\n        if self._recent:\n"
     "            target = min(target, min(x for _, x in self._recent) + p.max_ahead)",
     "pass  # SABOTAJE",
     PYTEST + ["tests/test_voice_brakes.py"]),
    ("frenos: un solo ciclo confirma", "tools/voice_brakes.py",
     "if prev is None:\n            return False",
     "if prev is None:\n            prev = target  # SABOTAJE",
     PYTEST + ["tests/test_voice_brakes.py", "tests/test_modos.py"]),
    ("Asistido acelera al ponerse al día", "frontend/core.js",
     "st.next = now + periodo;            // desde ahora, no desde el paso perdido",
     "st.next = st.next + periodo / 2;   // SABOTAJE",
     PYTEST + ["tests/test_frontend.py"]),
    ("orden mirror no cambia nada", "frontend/core.js",
     'case "mirror": return { ...p, espejo: !p.espejo };', 'case "mirror": return { ...p };  // SABOTAJE',
     PYTEST + ["tests/test_e2e_control.py"]),
    ("orden top no se ejecuta en el visor", "frontend/app.js",
     'case "top": resetPosition(); break;', 'case "top": break;  // SABOTAJE',
     PYTEST + ["tests/test_e2e_control.py"]),
    ("un control reemplaza al visor de /ws", "backend/server.py",
     "        self.controls.add(conn)\n", "        self.controls.add(conn)\n        self.conn = conn  # SABOTAJE\n",
     PYTEST + ["tests/test_control.py", "-k", "un_solo_visor"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    malos = 0
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
            code = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).returncode
        finally:
            path.write_bytes(original)
        restaurado = sha(path) == antes
        ok = code != 0 and restaurado
        malos += not ok
        print(f"{'✓' if ok else '✗'} {nombre}: exit {code} {'(rojo)' if code else '(VERDE: la puerta no ve el sabotaje)'}"
              f" · restaurado {'sí' if restaurado else 'NO'}")
    verde = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/"], cwd=ROOT, capture_output=True, text=True)
    print(f"{'✓' if verde.returncode == 0 else '✗'} vuelta a verde: exit {verde.returncode} · "
          f"{verde.stdout.strip().splitlines()[-1] if verde.stdout.strip() else ''}")
    malos += verde.returncode != 0
    return 1 if malos else 0


if __name__ == "__main__":
    sys.exit(main())
