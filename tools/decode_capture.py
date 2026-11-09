"""Decode a phase-2 mic capture (captures/mic-*.au) to a 64 kHz mono WAV.

Usage: tools/winpy.sh tools/decode_capture.py captures/mic-XXXX.au [--play] [--normalized]
--play opens the WAV with the Windows default player.
--normalized also writes <name>-normalized.wav, peak-normalized to -3 dBFS (listening aid
only; the raw WAV is the true level).
"""
import os
import pathlib
import struct
import sys
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from airpodlink.eld import ELD_STREAM_RATE, EldDecoder  # noqa: E402


def read_aus(path):
    data = pathlib.Path(path).read_bytes()
    off = 0
    while off + 6 <= len(data):
        ts, length = struct.unpack_from("<IH", data, off)
        off += 6
        yield ts, data[off:off + length]
        off += length


def write_wav(out: pathlib.Path, pcm: np.ndarray) -> None:
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ELD_STREAM_RATE)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    print(f"wrote {out}")


def main(path: str, play: bool, normalized: bool) -> None:
    decoder = EldDecoder()
    chunks = [decoder.decode(au) for _, au in read_aus(path)]
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    rms = float(np.sqrt(np.mean(pcm ** 2))) if pcm.size else 0.0
    out = pathlib.Path(path).with_suffix(".wav")
    print(f"{decoder.frames} AUs decoded, {decoder.errors} errors, {pcm.size} samples = "
          f"{pcm.size / ELD_STREAM_RATE:.2f} s @ {ELD_STREAM_RATE} Hz; peak {peak:.3f} "
          f"({20 * np.log10(peak + 1e-12):.1f} dBFS), rms {20 * np.log10(rms + 1e-12):.1f} dBFS")
    write_wav(out, pcm)
    if normalized and peak > 0:
        gain = 10 ** (-3 / 20) / peak
        out = out.with_name(out.stem + "-normalized.wav")
        print(f"normalized copy: gain x{gain:.1f} ({20 * np.log10(gain):+.1f} dB)")
        write_wav(out, pcm * gain)
    if play:
        # On a \\wsl.localhost path this can block behind a Windows "open file?" prompt.
        os.startfile(str(out))  # noqa: S606 - user asked to play it


if __name__ == "__main__":
    main(sys.argv[1], "--play" in sys.argv, "--normalized" in sys.argv)
