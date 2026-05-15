"""Hardware handshake: verifica CUDA + GPU disponible para faster-whisper."""
import sys

def main():
    try:
        import torch
    except ImportError:
        print("[X] torch no instalado. Ejecuta scripts/install.bat primero.")
        sys.exit(1)

    print(f"[i] torch version: {torch.__version__}")
    print(f"[i] CUDA disponible: {torch.cuda.is_available()}")

    if not torch.cuda.is_available():
        print("[X] CUDA no disponible. Revisa drivers NVIDIA y CUDA Toolkit.")
        sys.exit(1)

    print(f"[OK] CUDA version: {torch.version.cuda}")
    print(f"[OK] Dispositivos CUDA: {torch.cuda.device_count()}")
    for i in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(i)
        vram_gb = props.total_memory / (1024 ** 3)
        print(f"     #{i}: {props.name} | VRAM {vram_gb:.1f} GB | SM {props.major}.{props.minor}")

    try:
        from faster_whisper import WhisperModel  # noqa: F401
        print("[OK] faster-whisper importable.")
    except ImportError:
        print("[X] faster-whisper no instalado.")
        sys.exit(1)

    print("\n[OK] Handshake CUDA + faster-whisper completo.")

if __name__ == "__main__":
    main()
