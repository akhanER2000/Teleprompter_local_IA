"""Graba N segundos del micrófono a un WAV de 16 kHz mono (PCM 16 bits).

Uso:
  .venv312\\Scripts\\python.exe tools\\record_sample.py --name lectura_normal --seconds 90
  .venv312\\Scripts\\python.exe tools\\record_sample.py --list-devices

Los archivos quedan en tests/fixtures/audio/ (ignorada por git: contiene tu voz).
Si el dispositivo no acepta 16 kHz, graba a su tasa nativa y remuestrea con PyAV.
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "tests" / "fixtures" / "audio"
TARGET_SR = 16000


def _resample(samples: np.ndarray, src_sr: int) -> np.ndarray:
    import av  # instalado como dependencia de faster-whisper

    resampler = av.AudioResampler(format="flt", layout="mono", rate=TARGET_SR)
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="flt", layout="mono")
    frame.sample_rate = src_sr
    out = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    out += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


def _record(seconds: float, device, samplerate: int) -> np.ndarray:
    frames: list[np.ndarray] = []
    level = {"db": -120.0}

    def callback(indata, n_frames, time_info, status):  # hilo de PortAudio
        block = indata[:, 0].copy()
        frames.append(block)
        rms = float(np.sqrt(np.mean(block ** 2))) if block.size else 0.0
        level["db"] = 20 * np.log10(max(rms, 1e-6))

    with sd.InputStream(samplerate=samplerate, channels=1, dtype="float32",
                        blocksize=int(samplerate * 0.1), device=device,
                        callback=callback):
        t0 = time.time()
        try:
            while (elapsed := time.time() - t0) < seconds:
                db = level["db"]
                bar = "#" * int(max(0.0, min(40.0, (db + 60) * 40 / 60)))
                print(f"\r  {elapsed:5.1f}/{seconds:.0f} s  {db:6.1f} dBFS |{bar:<40}|", end="", flush=True)
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[i] Grabacion cortada por el usuario; se guarda lo grabado.", end="")
    print()
    return np.concatenate(frames) if frames else np.zeros(0, np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="nombre del archivo, sin extension (p. ej. lectura_normal)")
    ap.add_argument("--seconds", type=float, default=60.0, help="duracion en segundos (60 por defecto)")
    ap.add_argument("--device", default=None, help="indice o parte del nombre del dispositivo de entrada")
    ap.add_argument("--list-devices", action="store_true", help="lista los dispositivos de entrada y sale")
    ap.add_argument("--no-countdown", action="store_true", help="empieza a grabar sin cuenta regresiva")
    args = ap.parse_args()

    if args.list_devices:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  #{i:3d}  {d['name']}  ({int(d['default_samplerate'])} Hz)")
        return 0
    if not args.name:
        ap.error("falta --name")

    device = int(args.device) if args.device and args.device.isdigit() else args.device
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{args.name}.wav"
    if out_path.exists():
        print(f"[X] {out_path} ya existe. Usa otro --name o borra el archivo.")
        return 1

    info = sd.query_devices(device, "input")
    print(f"[i] Dispositivo: {info['name']}")
    if not args.no_countdown:
        for n in (3, 2, 1):
            print(f"  Empieza en {n}...", flush=True)
            time.sleep(1)
    print("[i] Grabando. Ctrl+C para cortar antes.")

    try:
        try:
            samples = _record(args.seconds, device, TARGET_SR)
        except sd.PortAudioError:
            native = int(info["default_samplerate"])
            print(f"[i] El dispositivo no acepta {TARGET_SR} Hz; grabo a {native} Hz y remuestreo.")
            samples = _resample(_record(args.seconds, device, native), native)
    except KeyboardInterrupt:
        print("\n[X] Cancelado, no se guardo nada.")
        return 1

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_SR)
        wf.writeframes(pcm.tobytes())
    peak_db = 20 * np.log10(max(float(np.max(np.abs(samples))) if samples.size else 0.0, 1e-6))
    print(f"[OK] {out_path} ({len(samples) / TARGET_SR:.1f} s, pico {peak_db:.1f} dBFS)")
    if peak_db < -30:
        print("[!] Nivel bajo: revisa la ganancia del micrófono en Windows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
