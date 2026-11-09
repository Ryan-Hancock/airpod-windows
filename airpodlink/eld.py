"""AAC-ELD decoder for the AirPods' AACP hi-res mic (PyAV / FFmpeg native AAC decoder, LGPL).

Config from librepods PR #655 (linux-rust/src/audio/eld.rs):
  AudioSpecificConfig F8 E6 30 00 = AOT 39 (ER AAC ELD), 48 kHz index, mono, 480-sample frames.
The stream actually runs at 64 kHz: the AirPods send 133.3 AUs/s x 480 samples = 64000
samples/s (measured, phase 2), and the PR likewise decodes with this config and plays the
output at 64 kHz. So: decode as configured, *interpret* the PCM as 64 kHz.

Licence/patents: FFmpeg's AAC decoder is LGPL; AAC-ELD is covered by patents (Via LA /
Fraunhofer). Personal use only, as noted in the README.
"""

from __future__ import annotations

import av
import numpy as np

ELD_ASC = bytes([0xF8, 0xE6, 0x30, 0x00])
ELD_CONFIG_RATE = 48000      # what the ASC says
ELD_STREAM_RATE = 64000      # what the PCM really is (480 samples per 7.5 ms AU)
ELD_FRAME_SAMPLES = 480


class EldDecoder:
    def __init__(self) -> None:
        self.ctx = av.CodecContext.create("aac", "r")
        self.ctx.extradata = ELD_ASC
        self.ctx.sample_rate = ELD_CONFIG_RATE
        self.ctx.layout = "mono"
        self.ctx.open()
        self.errors = 0
        self.frames = 0

    def decode(self, au: bytes) -> np.ndarray:
        """Decode one access unit to float32 mono samples (at ELD_STREAM_RATE).

        On a decode error returns one frame of silence, like the PR does, so timing holds.
        """
        try:
            out = [f.to_ndarray() for f in self.ctx.decode(av.Packet(au))]
        except av.error.FFmpegError:
            out = []
        if not out:
            self.errors += 1
            return np.zeros(ELD_FRAME_SAMPLES, dtype=np.float32)
        self.frames += 1
        pcm = np.concatenate([a.reshape(-1) if a.ndim == 1 else a[0] for a in out])
        return pcm.astype(np.float32, copy=False)
