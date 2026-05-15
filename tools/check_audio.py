"""Audio handshake: lista dispositivos de entrada y captura 1 segundo de prueba."""
import sys

def main():
    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        print("[X] sounddevice/numpy no instalados.")
        sys.exit(1)

    print("[i] Dispositivos de audio:")
    print(sd.query_devices())
    default_in, _ = sd.default.device
    print(f"\n[i] Dispositivo de entrada por defecto: #{default_in}")

    print("[i] Capturando 1 segundo a 16 kHz mono...")
    rec = sd.rec(int(1 * 16000), samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    rms = float(np.sqrt(np.mean(rec ** 2)))
    print(f"[OK] Capturado. RMS = {rms:.4f}")
    if rms < 0.0005:
        print("[!] Nivel muy bajo. Verifica que el micrófono está conectado y activo.")
    else:
        print("[OK] Micrófono recibe señal.")

if __name__ == "__main__":
    main()
