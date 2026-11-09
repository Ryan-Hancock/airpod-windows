"""Live hi-res mic pipeline: AACP 0x58 SDUs -> AAC-ELD decode -> 48 kHz -> virtual cable.

Threads (the audio hot path never runs on the Bumble/asyncio loop):
  asyncio loop     : AacpClient calls `feed_sdu()` -> bounded queue (drops if full, never blocks)
  "mic-decode"     : demux AUs, decode AAC-ELD (64 kHz mono), resample to 48 kHz, push to ring
  PortAudio thread : WASAPI output callback pulls 10 ms blocks from the ring; writes silence
                     when muted, before the stream starts, or on underrun.

Privacy: the virtual cable is readable by any app. Output is silence unless `live` (set by
the mic session once the AirPods acknowledge mic START, cleared on stop/disconnect) AND not
`muted` (the user's toggle, which survives reconnects) AND `worn` (at least one bud in an
ear, from the AirPods' ear detection; avoids handling noise and mirrors macOS). Underruns, decode errors and
shutdown also yield silence rather than stale audio.

Python jitter: decode of one 7.5 ms AU takes well under 1 ms, and PortAudio pulls from a
ring buffer, so GIL stalls up to the ring's depth are absorbed. Measured numbers go in the
README; if they're not good enough, this module is the piece to move to Rust/C.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time

import av
import numpy as np
import sounddevice as sd

from airpodlink.aacp import demux_audio_sdu
from airpodlink.eld import ELD_STREAM_RATE, EldDecoder

logger = logging.getLogger(__name__)

OUT_RATE = 48000
BLOCK = 480  # 10 ms at 48 kHz


class Ring:
    """Single-producer/single-consumer float32 ring with a max depth (drops oldest)."""

    def __init__(self, capacity: int) -> None:
        self.buf = np.zeros(capacity, dtype=np.float32)
        self.capacity = capacity
        self.read = 0
        self.size = 0
        self.lock = threading.Lock()
        self.dropped = 0
        self.underruns = 0

    def push(self, x: np.ndarray) -> None:
        with self.lock:
            n = len(x)
            if n > self.capacity:
                x = x[-self.capacity:]
                n = self.capacity
            overflow = self.size + n - self.capacity
            if overflow > 0:  # drop oldest to bound latency
                self.read = (self.read + overflow) % self.capacity
                self.size -= overflow
                self.dropped += overflow
            w = (self.read + self.size) % self.capacity
            first = min(n, self.capacity - w)
            self.buf[w:w + first] = x[:first]
            self.buf[:n - first] = x[first:]
            self.size += n

    def pull(self, out: np.ndarray) -> int:
        with self.lock:
            n = min(len(out), self.size)
            first = min(n, self.capacity - self.read)
            out[:first] = self.buf[self.read:self.read + first]
            out[first:n] = self.buf[:n - first]
            self.read = (self.read + n) % self.capacity
            self.size -= n
            return n

    def clear(self) -> None:
        with self.lock:
            self.read = 0
            self.size = 0


class MicPipeline:
    # Jitter-buffer target. Measured (phase 5): SDUs arrive up to 52-67 ms late at p99 and
    # 66-83 ms worst-case vs their 30 ms cadence; 40 ms underran ~1x per 1-3 min live.
    # 10-min simulations (measured-like jitter, -109 ppm drift): 60 ms -> 11 underruns,
    # 70 ms -> 3, 80 ms -> 0 (80 ms without drift control -> 2). Trough setpoint = target - 30.
    DEFAULT_TARGET_MS = 80.0
    # Drift control: hold the buffer at target by resampling the output by up to +-1000 ppm
    # (< 2 cents pitch, inaudible). Measured mic-clock drift vs this PC: -109 ppm, which
    # without control drains a 45 ms buffer in ~7 min (one re-prefill glitch per ~7 min).
    DRIFT_GAIN_PPM_PER_MS = 20.0
    DRIFT_LIMIT_PPM = 1000.0

    def __init__(self, device: str | None = None, target_ms: float = DEFAULT_TARGET_MS,
                 max_buffer_ms: float = 200.0, gain_db: float = 0.0) -> None:
        self.device_name = device  # None -> airpodlink.devices.MIC_FEED
        self.target_ms = target_ms
        self.prefill = int(OUT_RATE * target_ms / 1000)
        # Drift control regulates the buffer *trough* (min level per 0.5 s window), not the
        # mean: the level saw-tooths by one 30 ms SDU, and steering the mean to target pushed
        # the troughs toward empty (simulation: 2x the underruns). Setpoint = target - 30 ms.
        self.level_ms_avg = target_ms - 30.0  # EMA of per-window trough level
        self._win_min = None
        self._win_n = 0
        self.ratio_ppm = 0.0            # current output resample correction
        self._frac = 0.0                # fractional-sample accumulator for the resampler
        self.ring = Ring(int(OUT_RATE * max_buffer_ms / 1000))
        self.gain = float(10 ** (gain_db / 20))
        self.sdu_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self.muted = False         # user mute toggle (persists across reconnects)
        self.live = False          # session gate: True only while the AirPods mic is streaming
        self.worn = True           # ear gate: False when no bud is in an ear (like macOS)
        self.primed = False        # wait for `prefill` samples before playing
        self.stats_lock = threading.Lock()
        self.sdus_in = 0
        self.sdus_dropped = 0
        self.decode_errors = 0
        self.decode_time_max = 0.0
        self.peak = 0.0
        self._thread: threading.Thread | None = None
        self._stream: sd.OutputStream | None = None
        self._last_cb = time.perf_counter()

    # -------------------------------------------------------------- producer side
    def feed_sdu(self, sdu: bytes) -> None:
        """Called on the asyncio loop for each 0x58 audio SDU. Never blocks."""
        self.sdus_in += 1
        try:
            self.sdu_queue.put_nowait(sdu)
        except queue.Full:
            self.sdus_dropped += 1

    def _decode_loop(self) -> None:
        decoder = EldDecoder()
        resampler = av.AudioResampler(format="flt", layout="mono", rate=OUT_RATE)
        pts = 0
        while (sdu := self.sdu_queue.get()) is not None:
            t0 = time.perf_counter()
            aus, _ = demux_audio_sdu(sdu)
            pcm = np.concatenate([decoder.decode(au) for _, au in aus]) if aus else None
            if pcm is None:
                continue
            frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="flt", layout="mono")
            frame.sample_rate = ELD_STREAM_RATE
            frame.pts = pts
            pts += pcm.size
            out = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
            if out:
                y = np.concatenate(out) * self.gain
                self.ring.push(y)
                self.peak = max(self.peak, float(np.max(np.abs(y))))
            dt = time.perf_counter() - t0
            with self.stats_lock:
                self.decode_errors = decoder.errors
                self.decode_time_max = max(self.decode_time_max, dt)

    # -------------------------------------------------------------- consumer side
    def _callback(self, outdata, frames, _time, status) -> None:
        self._last_cb = time.perf_counter()
        if status:
            logger.debug("PortAudio status: %s", status)
        mono = np.zeros(frames, dtype=np.float32)
        if not self.primed and self.ring.size >= self.prefill:
            self.primed = True
        if self.primed:
            # Drift control: consume slightly more (buffer above target) or fewer samples
            # than we output, via linear interpolation. EMA over ~100 callbacks (~1 s) so
            # arrival jitter (30 ms SDU bursts) doesn't modulate the pitch.
            level_ms = self.ring.size * 1000 / OUT_RATE
            self._win_min = level_ms if self._win_min is None else min(self._win_min, level_ms)
            self._win_n += 1
            if self._win_n >= 50:  # every 0.5 s
                self.level_ms_avg += (self._win_min - self.level_ms_avg) * 0.2
                self._win_min, self._win_n = None, 0
                setpoint = self.target_ms - 30.0
                self.ratio_ppm = float(np.clip((self.level_ms_avg - setpoint) * self.DRIFT_GAIN_PPM_PER_MS,
                                               -self.DRIFT_LIMIT_PPM, self.DRIFT_LIMIT_PPM))
            need = frames * (1 + self.ratio_ppm * 1e-6) + self._frac
            m = int(need)
            self._frac = need - m
            src = np.zeros(m + 1, dtype=np.float32)
            n = self.ring.pull(src[:m])
            if n < m:
                self.ring.underruns += 1
                self.primed = False  # re-prefill after an underrun instead of crackling
            else:
                src[m] = src[m - 1]  # hold the last sample for the interpolation edge
                mono[:] = np.interp(np.arange(frames) * (m / frames), np.arange(m + 1), src)
        if self.muted or not self.live or not self.worn:
            mono[:] = 0.0
        outdata[:] = np.repeat(mono[:, None], outdata.shape[1], axis=1)

    # -------------------------------------------------------------- lifecycle
    def _find_device(self) -> int:
        from airpodlink.devices import MIC_FEED, pick, wasapi_names

        name = self.device_name or pick(MIC_FEED, wasapi_names("output"))
        wasapi = next(i for i, api in enumerate(sd.query_hostapis()) if "WASAPI" in api["name"])
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == wasapi and d["max_output_channels"] > 0 and d["name"] == name:
                return i
        raise RuntimeError(f"WASAPI output device {name!r} not found "
                           "(is VB-Cable installed?)")

    def start(self) -> str:
        self._thread = threading.Thread(target=self._decode_loop, name="mic-decode", daemon=True)
        self._thread.start()
        return self._open_output()

    def _open_output(self) -> str:
        index = self._find_device()
        info = sd.query_devices(index)
        # auto_convert: WASAPI shared mode converts our 48 kHz float to whatever format the user
        # set on the endpoint in Sound settings (otherwise a non-48 kHz endpoint refuses to open).
        self._stream = sd.OutputStream(device=index, samplerate=OUT_RATE, channels=2,
                                       dtype="float32", blocksize=BLOCK, latency="low",
                                       extra_settings=sd.WasapiSettings(auto_convert=True),
                                       callback=self._callback)
        self._last_cb = time.perf_counter()
        self._stream.start()
        return f"{info['name']} @ {OUT_RATE} Hz, output latency {self._stream.latency * 1000:.1f} ms"

    def watchdog(self) -> str | None:
        """Call ~1x/s. Windows silently tears down the stream when the endpoint's format changes
        (callbacks just stop); reopen it."""
        if self._stream is None or time.perf_counter() - self._last_cb < 1.5:
            return None
        try:
            with contextlib.suppress(Exception):
                self._stream.close()
            return f"mic output stalled (device format change?) -> reopened: {self._open_output()}"
        except Exception as error:  # noqa: BLE001 - retry next tick
            self._last_cb = time.perf_counter()
            return f"mic output stalled; reopen failed ({error!r}), retrying"

    def reset(self) -> None:
        """Drop buffered audio (e.g. after a stream restart) and re-prefill."""
        self.ring.clear()
        self.primed = False
        self.level_ms_avg = self.target_ms - 30.0
        self._win_min, self._win_n = None, 0
        self._frac = 0.0

    def stop(self) -> None:
        self.live = False
        self.sdu_queue.put(None)
        if self._thread:
            self._thread.join(timeout=2)
        if self._stream:
            self._stream.stop()
            self._stream.close()

    def report(self) -> str:
        with self.stats_lock:
            line = (f"in {self.sdus_in} SDUs (dropped {self.sdus_dropped}), decode errors "
                    f"{self.decode_errors}, max decode {self.decode_time_max * 1000:.2f} ms/SDU, "
                    f"buffer {self.ring.size * 1000 // OUT_RATE} ms, underruns {self.ring.underruns}, "
                    f"overflow-dropped {self.ring.dropped * 1000 // OUT_RATE} ms, "
                    f"trough {self.level_ms_avg:.0f}/{self.target_ms - 30:.0f} ms, drift corr {self.ratio_ppm:+.0f} ppm, "
                    f"peak {20 * np.log10(self.peak + 1e-9):.1f} dBFS, "
                    f"{'MUTED' if self.muted else 'not worn' if not self.worn else 'live' if self.live else 'idle'}")
            self.decode_time_max = 0.0
            self.peak = 0.0
        return line
