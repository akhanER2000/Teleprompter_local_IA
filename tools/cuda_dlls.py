"""Registra las DLL de CUDA instaladas por pip (nvidia-*-cu12) para CTranslate2.

Sin torch, CTranslate2 no encuentra cublas64_12.dll: el paquete
nvidia-cublas-cu12 la deja en site-packages/nvidia/cublas/bin, fuera del PATH.
Hay que llamar a register_cuda_dlls() antes de cargar un modelo en CUDA.
"""
from __future__ import annotations

import glob
import os
import sys

_registered: list[str] = []


def register_cuda_dlls() -> list[str]:
    """Agrega cada site-packages/nvidia/*/bin al buscador de DLL. Idempotente.

    Usa os.add_dll_directory y además antepone la carpeta al PATH, porque
    CTranslate2 carga cuBLAS de forma diferida con LoadLibrary, que solo
    respeta el PATH.
    """
    if sys.platform != "win32" or _registered:
        return list(_registered)
    candidates: list[str] = []
    for base in sys.path:
        if base and os.path.basename(base).lower() == "site-packages":
            candidates.extend(glob.glob(os.path.join(base, "nvidia", "*", "bin")))
    for path in sorted(set(candidates)):
        try:
            os.add_dll_directory(path)
        except (FileNotFoundError, OSError):
            continue
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
        _registered.append(path)
    return list(_registered)


if __name__ == "__main__":
    for p in register_cuda_dlls():
        print(p)
