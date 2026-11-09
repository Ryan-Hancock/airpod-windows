"""Generate A2DP test files: media/test.sbc (raw SBC) and media/test.aac (ADTS AAC-LC).

The 30 s pattern makes channel mapping and dropouts easy to hear:
  0-10 s : 1 s beeps alternating LEFT (440 Hz) / RIGHT (660 Hz)
  10-20 s: continuous 1 kHz tone on both channels (dropouts/clicks stand out)
  20-30 s: log sweep 100 Hz -> 12 kHz on both channels
"""
import pathlib

import av
import numpy as np

RATE = 44100
OUT = pathlib.Path(__file__).resolve().parents[1] / "media"


def signal() -> np.ndarray:
    t = np.arange(10 * RATE) / RATE
    gate = ((t % 1.0) < 0.6).astype(np.float32)
    left = np.where((t.astype(int) % 2) == 0, np.sin(2 * np.pi * 440 * t) * gate, 0)
    right = np.where((t.astype(int) % 2) == 1, np.sin(2 * np.pi * 660 * t) * gate, 0)
    part1 = np.stack([left, right])
    tone = np.sin(2 * np.pi * 1000 * t)
    part2 = np.stack([tone, tone])
    f0, f1 = 100.0, 12000.0
    k = np.log(f1 / f0) / 10
    sweep = np.sin(2 * np.pi * f0 * (np.exp(k * t) - 1) / k)
    part3 = np.stack([sweep, sweep])
    return (0.3 * np.concatenate([part1, part2, part3], axis=1)).astype(np.float32)


def encode(path: pathlib.Path, fmt: str, codec: str, bit_rate: int, sample_fmt: str) -> None:
    pcm = signal()
    with av.open(str(path), "w", format=fmt) as container:
        stream = container.add_stream(codec, rate=RATE, layout="stereo")
        stream.bit_rate = bit_rate
        resampler = av.AudioResampler(format=sample_fmt, layout="stereo", rate=RATE)
        step = 4096
        for i in range(0, pcm.shape[1], step):
            frame = av.AudioFrame.from_ndarray(pcm[:, i : i + step].copy(), format="fltp", layout="stereo")
            frame.sample_rate = RATE
            for rframe in resampler.resample(frame):
                for packet in stream.encode(rframe):
                    container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    # SBC: 44.1 kHz joint stereo. AirPods Pro 3 advertise max bitpool 27; FFmpeg derives the
    # bitpool from the bitrate, and 184 kbit/s gives bitpool 27 (66-byte frames). Higher
    # bitpools exceed the sink's advertised range.
    encode(OUT / "test.sbc", "sbc", "sbc", 184_000, "s16")
    # AAC-LC 256 kbit/s in ADTS framing (what Bumble's AacParser expects).
    encode(OUT / "test.aac", "adts", "aac", 256_000, "fltp")
