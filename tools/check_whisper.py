"""STT ping: carga faster-whisper en CUDA y transcribe 2 segundos del micrófono."""
import sys
import time

def main():
    try:
        import sounddevice as sd
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError as e:
        print(f"[X] Falta dependencia: {e}")
        sys.exit(1)

    model_size = "medium"
    print(f"[i] Cargando modelo '{model_size}' en CUDA float16...")
    t0 = time.time()
    model = WhisperModel(model_size, device="cuda", compute_type="float16")
    print(f"[OK] Modelo cargado en {time.time() - t0:.1f} s.")

    print("[i] Habla durante 3 segundos...")
    rec = sd.rec(int(3 * 16000), samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    audio = np.squeeze(rec)

    print("[i] Transcribiendo...")
    t0 = time.time()
    segments, info = model.transcribe(audio, language="es", vad_filter=True, beam_size=1)
    text = " ".join(seg.text for seg in segments).strip()
    elapsed_ms = (time.time() - t0) * 1000

    print(f"[OK] Transcripción ({elapsed_ms:.0f} ms): {text!r}")
    print(f"[OK] Idioma detectado: {info.language} (prob {info.language_probability:.2f})")

if __name__ == "__main__":
    main()
