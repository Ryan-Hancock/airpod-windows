"""Live A2DP media source: Windows WASAPI loopback capture -> SBC/AAC encode -> RTP packets.

Default: WASAPI *loopback* of the AirPods playback endpoint (airpodlink.devices.PLAYBACK:
"AirPods Pro" after renaming it in Sound settings, originally "Voicemeeter VAIO3 Input"; verified
2026-10-02: Voicemeeter's VAIO endpoints consume audio in real time and loop back cleanly
even when the Voicemeeter app is not running). Make that endpoint the Windows default output
(optionally renamed "AirPods Pro") and every app plays to the AirPods. Any other render
endpoint works too (`device=`), and `loopback=False` captures a normal input instead
(e.g. "Voicemeeter Out B1").

Threads (hot path off the Bumble/asyncio loop):
  PortAudio capture callback : copies 10 ms float32 blocks into a bounded queue
  "a2dp-encode" thread        : encodes (PyAV/FFmpeg) and hands finished RTP packets to the
                                asyncio loop via call_soon_threadsafe
  asyncio loop               : MediaPacketPump sends packets as they arrive

Latency traps handled:
  - MediaPacketPump paces packets by `timestamp_seconds` against the wall clock. For a live
    source that only adds delay and drifts against the capture clock, so live packets carry
    timestamp_seconds = 0 and go out on arrival (the RTP timestamp itself stays sample-based).
  - Audio captured during connection setup (AVRCP, chime delay: ~2 s) is discarded when the
    pump starts pulling, otherwise it would become permanent extra latency.

RTP timestamps use the sample clock (A2DP spec), not Bumble's milliseconds.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time

import av
import numpy as np
import sounddevice as sd

from bumble.a2dp import (
    A2DP_MPEG_2_4_AAC_CODEC_TYPE,
    A2DP_NON_A2DP_CODEC_TYPE,
    VendorSpecificMediaCodecInformation,
    A2DP_SBC_CODEC_TYPE,
    AacMediaCodecInformation,
    SbcMediaCodecInformation,
)
from bumble.avdtp import AVDTP_AUDIO_MEDIA_TYPE, MediaCodecCapabilities
from bumble.codecs import AacAudioRtpPacket
from bumble.rtp import MediaPacket

logger = logging.getLogger(__name__)

RATE = 48000
CAPTURE_BLOCK = 480  # 10 ms
SBC_BITRATE = 198_000  # -> bitpool 27 at 48 kHz stereo (AirPods' max), 66-byte frames
AAC_BITRATE = 256_000  # AirPods' advertised max
RTP_HEADER = 12
APPLE_VENDOR_ID = 0x004C
APPLE_AAC_ELD = 0x8001
# SET_CONFIGURATION value macOS uses (via the Pico port's aac-eld-apple.md): object type 0x0080
# (AAC-ELD), 48 kHz (0x008) | stereo (0x4), VBR flag, max bitrate 256000.
ELD_CONFIG = bytes([0x00, 0x80, 0x00, 0x84, 0x00, 0x83, 0xE8, 0x00])
ELD_BITRATE = 256_000      # 320 B/frame; Pico uses 265k CBR, macOS measured ~227k
ELD_MAX_PAYLOAD = 986      # Apple's negotiated RTP payload limit for this codec
GAP_FILL_AFTER = 0.05  # s without capture data before the encoder starts feeding silence


def open_loopback(name: str, callback):
    """Open WASAPI loopback capture of render endpoint `name` (PyAudioWPatch). Returns (pa, stream, info)."""
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    info = next((d for d in pa.get_loopback_device_info_generator()
                 if d["name"].lower().startswith(name.lower())), None)
    if info is None:
        pa.terminate()
        raise RuntimeError(f"no WASAPI loopback endpoint for render device {name!r}")
    # Shared-mode loopback must use the endpoint's mix format, whatever the user picked in
    # Sound settings (44.1/48/96/192 kHz, any bit depth); we resample to 48 kHz later.
    rate = int(info["defaultSampleRate"])
    channels = info["maxInputChannels"]

    def on_data(data, frames, _time_info, status):
        block = np.frombuffer(data, dtype=np.float32).reshape(frames, channels)
        callback(block[:, :2] if channels >= 2 else np.repeat(block, 2, axis=1), frames, None, status, rate)
        return (None, pyaudio.paContinue)

    stream = pa.open(format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
                     input_device_index=info["index"], frames_per_buffer=rate // 100,
                     stream_callback=on_data)
    return pa, stream, info


def find_wasapi_input(name: str) -> int:
    wasapi = next(i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"])
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == wasapi and d["max_input_channels"] >= 2 and name.lower() in d["name"].lower():
            return i
    raise RuntimeError(f"WASAPI capture device containing {name!r} not found")


class _To48k:
    """Resample (frames, 2) float32 blocks from any capture rate to 48 kHz (A2DP rate).
    Pass-through at 48 kHz; the resampler is rebuilt when the capture rate changes."""

    def __init__(self) -> None:
        self.rate = RATE
        self.resampler = None
        self.pts = 0

    def __call__(self, block: np.ndarray, rate: int) -> np.ndarray:
        if rate == RATE:
            self.rate, self.resampler = RATE, None
            return block
        if rate != self.rate or self.resampler is None:
            self.rate, self.pts = rate, 0
            self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=RATE)
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(block.T), format="fltp", layout="stereo")
        frame.sample_rate = rate
        frame.pts = self.pts
        self.pts += block.shape[0]
        out = [f.to_ndarray().reshape(-1, 2) for f in self.resampler.resample(frame)]
        return np.concatenate(out) if out else np.zeros((0, 2), np.float32)


class LiveSource:
    def __init__(self, codec: str = "aac", device: str | None = None,
                 loopback: bool = True, sbc_frames_per_packet: int = 5, aac_encoder: str = "fdk",
                 eld_frames_per_packet: int = 1, eld_bitrate: int = ELD_BITRATE) -> None:
        if codec not in ("aac", "sbc", "eld"):
            raise ValueError("codec must be 'aac', 'sbc' or 'eld'")
        self.codec = codec
        self.codec_type = {"aac": A2DP_MPEG_2_4_AAC_CODEC_TYPE, "sbc": A2DP_SBC_CODEC_TYPE,
                           "eld": A2DP_NON_A2DP_CODEC_TYPE}[codec]
        # Apple AAC-ELD is an A2DP vendor codec (vendor 0x004C, codec 0x8001)
        self.vendor_id, self.vendor_codec_id = (APPLE_VENDOR_ID, APPLE_AAC_ELD) if codec == "eld" else (0, 0)
        self.eld_frames_per_packet = eld_frames_per_packet
        self.eld_bitrate = eld_bitrate
        if device is None:  # the AirPods playback endpoint (see airpodlink.devices)
            from airpodlink.devices import PLAYBACK, pick, wasapi_names
            device = pick(PLAYBACK, wasapi_names("output"))
        self.device_name = device
        self.loopback = loopback
        # "fdk" (Fraunhofer, vendor/fdk-aac) or "ffmpeg" (PyAV built-in; audibly weaker)
        self.aac_encoder = aac_encoder
        self._pa = None
        # SBC: 128 samples (2.67 ms) per frame; 5 frames = 13.3 ms per RTP packet.
        self.sbc_frames_per_packet = sbc_frames_per_packet
        self.capture_q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=50)  # 500 ms max
        self.packet_q: asyncio.Queue[MediaPacket | None] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stream: sd.InputStream | None = None
        self.thread: threading.Thread | None = None
        self.mtu = 1023
        self.capture_overflows = 0
        self.capture_restarts = 0
        self.gap_filled_ms = 0
        self._fill_clock = 0.0
        self.capture_rate = RATE
        self.last_capture = time.perf_counter()
        self.captured_blocks = 0
        self.packets_out = 0
        self.encode_max = 0.0
        self.peak = 0.0
        self.handoff_ms: list[float] = []

    def __str__(self) -> str:
        enc = f" ({self.aac_encoder})" if self.codec == "aac" else ""
        return f"live {self.codec.upper()}{enc} from '{self.device_name}'{' (loopback)' if self.loopback else ''}"

    async def capabilities(self) -> MediaCodecCapabilities:
        if self.codec == "eld":
            return MediaCodecCapabilities(
                media_type=AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=A2DP_NON_A2DP_CODEC_TYPE,
                media_codec_information=VendorSpecificMediaCodecInformation(APPLE_VENDOR_ID, APPLE_AAC_ELD, ELD_CONFIG))
        if self.codec == "aac":
            info = AacMediaCodecInformation(
                object_type=AacMediaCodecInformation.ObjectType.MPEG_2_AAC_LC,
                sampling_frequency=AacMediaCodecInformation.SamplingFrequency.SF_48000,
                channels=AacMediaCodecInformation.Channels.STEREO,
                vbr=1, bitrate=AAC_BITRATE)
            return MediaCodecCapabilities(media_type=AVDTP_AUDIO_MEDIA_TYPE,
                                          media_codec_type=A2DP_MPEG_2_4_AAC_CODEC_TYPE,
                                          media_codec_information=info)
        info = SbcMediaCodecInformation(
            sampling_frequency=SbcMediaCodecInformation.SamplingFrequency.SF_48000,
            channel_mode=SbcMediaCodecInformation.ChannelMode.STEREO,  # what FFmpeg's encoder emits
            block_length=SbcMediaCodecInformation.BlockLength.BL_16,
            subbands=SbcMediaCodecInformation.Subbands.S_8,
            allocation_method=SbcMediaCodecInformation.AllocationMethod.LOUDNESS,
            minimum_bitpool_value=2, maximum_bitpool_value=27)
        return MediaCodecCapabilities(media_type=AVDTP_AUDIO_MEDIA_TYPE,
                                      media_codec_type=A2DP_SBC_CODEC_TYPE,
                                      media_codec_information=info)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.packet_q = asyncio.Queue()
        self.thread = threading.Thread(target=self._encode_loop, name="a2dp-encode", daemon=True)
        self.thread.start()
        self._open_capture()

    def _open_capture(self) -> None:
        self.last_capture = time.perf_counter()
        if self.loopback:
            self._pa, self.stream, info = open_loopback(self.device_name, self._on_capture)
            self.capture_rate = int(info["defaultSampleRate"])
            self.stream.start_stream()
            self.capture_latency = self.stream.get_input_latency()
        else:
            index = find_wasapi_input(self.device_name)
            self.capture_rate = RATE
            self.stream = sd.InputStream(device=index, samplerate=RATE, channels=2, dtype="float32",
                                         blocksize=CAPTURE_BLOCK, latency="low", callback=self._on_capture)
            self.stream.start()
            self.capture_latency = self.stream.latency

    def _close_capture(self) -> None:
        if not self.stream:
            return
        try:
            if self.loopback:
                self.stream.stop_stream()
                self.stream.close()
                self._pa.terminate()
            else:
                self.stream.stop()
                self.stream.close()
        except Exception:  # noqa: BLE001 - an invalidated stream may refuse to stop cleanly
            pass
        self.stream = None

    async def watchdog(self) -> str | None:
        """Call ~1x/s. Windows silently tears down open streams when the endpoint's format is
        changed (or the device restarts); capture then just stops. Reopen it with the new format."""
        if time.perf_counter() - self.last_capture < 1.5:
            return None
        old = getattr(self, "capture_rate", None)
        await asyncio.to_thread(self._close_capture)
        try:
            await asyncio.to_thread(self._open_capture)
        except Exception as error:  # noqa: BLE001 - retry next tick
            self.last_capture = time.perf_counter()
            return f"capture stalled; reopen failed ({error!r}), retrying"
        self.capture_restarts += 1
        return (f"capture stalled (device format change?) -> reopened at {self.capture_rate} Hz"
                f"{'' if old == self.capture_rate else f' (was {old} Hz)'}")

    async def stop(self) -> None:
        self._close_capture()
        self.capture_q.put(None)
        if self.thread:
            self.thread.join(timeout=2)

    def _on_capture(self, indata, frames, _time, status, rate: int = RATE) -> None:
        if status:
            logger.debug("capture status: %s", status)
        self.last_capture = time.perf_counter()
        try:
            self.capture_q.put_nowait((indata.copy(), self.last_capture, rate))
            self.captured_blocks += 1
        except queue.Full:
            self.capture_overflows += 1

    # ------------------------------------------------------------------ encoding
    def _emit(self, packet: MediaPacket) -> None:
        self.loop.call_soon_threadsafe(self.packet_q.put_nowait, packet)
        self.packets_out += 1

    def _next_block(self):
        """Next capture block, or silence while the capture is stalled.

        When Windows tears down the loopback stream (format change), capture stops for ~1.5 s
        until the watchdog reopens it. Sending nothing meanwhile left the RTP clock behind real
        time: afterwards the AirPods treated every packet as late and stayed silent (observed,
        no SUSPEND). So during a stall we keep feeding silence at real-time pace: the AirPods'
        buffer never runs dry and RTP timestamps stay locked to wall-clock time.
        """
        try:
            item = self.capture_q.get(timeout=GAP_FILL_AFTER)
            self._fill_clock = 0.0  # real capture (or shutdown): stop gap-filling
            return item
        except queue.Empty:
            pass
        now = time.perf_counter()
        frames = int(RATE * (now - self._fill_clock)) if self._fill_clock else int(RATE * GAP_FILL_AFTER)
        self._fill_clock = now
        frames = max(CAPTURE_BLOCK, min(frames, RATE // 2))
        self.gap_filled_ms += frames * 1000 // RATE
        return np.zeros((frames, 2), np.float32), now, RATE

    def _encode_loop(self) -> None:
        if self.codec == "eld":
            return self._encode_loop_eld()
        if self.codec == "aac" and self.aac_encoder == "fdk":
            return self._encode_loop_fdk()
        fmt = "fltp" if self.codec == "aac" else "s16"
        ctx = av.CodecContext.create(self.codec, "w")
        ctx.sample_rate = RATE
        ctx.layout = "stereo"
        ctx.format = fmt
        ctx.bit_rate = AAC_BITRATE if self.codec == "aac" else SBC_BITRATE
        ctx.open()
        # Feed the encoder exactly frame_size samples at a time (AAC needs 1024).
        frame_size = ctx.frame_size or 128
        fifo = av.AudioFifo()
        resampler = av.AudioResampler(format=fmt, layout="stereo", rate=RATE)
        sample_clock = 0        # samples handed to the encoder
        rtp_samples = 0         # samples already emitted in RTP packets
        seq = 0
        sbc_frames: list[bytes] = []
        max_sbc_payload = self.mtu - RTP_HEADER - 1

        to48 = _To48k()
        while (item := self._next_block()) is not None:
            block, block_arrival, rate = item
            t0 = time.perf_counter()
            self.peak = max(self.peak, float(np.max(np.abs(block))))
            block = to48(block, rate)
            if not len(block):
                continue
            frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(block.T), format="fltp", layout="stereo")
            frame.sample_rate = RATE
            for rf in resampler.resample(frame):
                fifo.write(rf)
            while (chunk := fifo.read(frame_size)) is not None:
                chunk.pts = sample_clock
                sample_clock += chunk.samples
                for pkt in ctx.encode(chunk):
                    data = bytes(pkt)
                    if self.codec == "aac":
                        payload = bytes(AacAudioRtpPacket.for_simple_aac(RATE, 2, data))
                        self._emit(self._rtp(seq, rtp_samples, payload, block_arrival))
                        seq += 1
                        rtp_samples += frame_size
                    else:
                        sbc_frames.append(data)
                        size = sum(map(len, sbc_frames))
                        if (len(sbc_frames) >= self.sbc_frames_per_packet
                                or size + len(data) > max_sbc_payload):
                            payload = bytes([len(sbc_frames)]) + b"".join(sbc_frames)
                            self._emit(self._rtp(seq, rtp_samples, payload, block_arrival))
                            seq += 1
                            rtp_samples += 128 * len(sbc_frames)
                            sbc_frames = []
            self.encode_max = max(self.encode_max, time.perf_counter() - t0)

        self.loop.call_soon_threadsafe(self.packet_q.put_nowait, None)

    def _encode_loop_fdk(self) -> None:
        """AAC-LC via fdk-aac: CBR 256 kbit/s (the AirPods' advertised max), afterburner on."""
        from airpodlink.fdk_aac import AOT_AAC_LC, FdkAacEncoder

        enc = FdkAacEncoder(aot=AOT_AAC_LC, rate=RATE, channels=2, bitrate=AAC_BITRATE, bitrate_mode=0)
        seq = 0
        rtp_samples = 0
        try:
            to48 = _To48k()
            while (item := self._next_block()) is not None:
                block, block_arrival, rate = item
                t0 = time.perf_counter()
                self.peak = max(self.peak, float(np.max(np.abs(block))))
                block = to48(block, rate)
                if not len(block):
                    continue
                pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype(np.int16)  # (frames, 2) interleaved
                for au in enc.encode(pcm):
                    payload = bytes(AacAudioRtpPacket.for_simple_aac(RATE, 2, au))
                    self._emit(self._rtp(seq, rtp_samples, payload, block_arrival))
                    seq += 1
                    rtp_samples += enc.frame_length
                self.encode_max = max(self.encode_max, time.perf_counter() - t0)
        finally:
            enc.close()
            self.loop.call_soon_threadsafe(self.packet_q.put_nowait, None)

    def _encode_loop_eld(self) -> None:
        """Apple AAC-ELD (vendor codec 0x004C:0x8001), per han-um's Pico port / macOS:
        fdk AOT 39, 48 kHz stereo, 480-sample frames, SBR off, CBR, raw AUs. RTP payload =
        frames back to back, each prefixed by Apple's 4-byte size header
        [0xB0 | seq>>8, seq & 0xFF, 0x10 | len>>8, len & 0xFF] (12-bit frame sequence from 1),
        payload <= 986 bytes, RTP timestamp += 480 per frame."""
        from airpodlink.fdk_aac import AOT_ER_AAC_ELD, FdkAacEncoder

        enc = FdkAacEncoder(aot=AOT_ER_AAC_ELD, rate=RATE, channels=2, bitrate=self.eld_bitrate,
                            bitrate_mode=0, granule=480, afterburner=False)
        seq, frame_seq, rtp_samples = 0, 1, 0
        frames: list[bytes] = []
        first_arrival = None
        try:
            to48 = _To48k()
            while (item := self._next_block()) is not None:
                block, block_arrival, rate = item
                t0 = time.perf_counter()
                self.peak = max(self.peak, float(np.max(np.abs(block))))
                block = to48(block, rate)
                if not len(block):
                    continue
                pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype(np.int16)
                for au in enc.encode(pcm):
                    if len(au) > 0x7FF:
                        continue  # cannot be described by the 12-bit size field
                    framed = bytes([0xB0 | (frame_seq >> 8) & 0x0F, frame_seq & 0xFF,
                                    0x10 | (len(au) >> 8) & 0x0F, len(au) & 0xFF]) + au
                    frame_seq = (frame_seq + 1) & 0x0FFF
                    if frames and sum(map(len, frames)) + len(framed) > ELD_MAX_PAYLOAD:
                        self._emit(self._rtp(seq, rtp_samples, b"".join(frames), first_arrival))
                        seq += 1
                        rtp_samples += enc.frame_length * len(frames)
                        frames = []
                    if not frames:
                        first_arrival = block_arrival
                    frames.append(framed)
                    if len(frames) >= self.eld_frames_per_packet:
                        self._emit(self._rtp(seq, rtp_samples, b"".join(frames), block_arrival))
                        seq += 1
                        rtp_samples += enc.frame_length * len(frames)
                        frames = []
                self.encode_max = max(self.encode_max, time.perf_counter() - t0)
        finally:
            enc.close()
            self.loop.call_soon_threadsafe(self.packet_q.put_nowait, None)

    @staticmethod
    def _rtp(seq: int, samples: int, payload: bytes, captured_at: float) -> MediaPacket:
        packet = MediaPacket(2, 0, 0, 0, seq & 0xFFFF, samples & 0xFFFFFFFF, 0, [], 96, payload)
        packet.timestamp_seconds = 0.0  # live: send on arrival (see module docstring)
        # perf_counter() when the capture block that *completed* this packet arrived. The
        # oldest audio in the packet is older by up to one codec frame (AAC 21.3 ms,
        # SBC 5 x 2.67 ms) - that framing delay is reported separately.
        packet.captured_at = captured_at
        return packet

    def packets(self, mtu: int):
        self.mtu = mtu

        async def generate():
            stale = 0
            while not self.packet_q.empty():  # drop audio captured during setup
                self.packet_q.get_nowait()
                stale += 1
            if stale:
                logger.info("dropped %d packets captured before the stream started", stale)
            while (packet := await self.packet_q.get()) is not None:
                # MediaPacketPump writes to L2CAP right after we yield (live: no pacing wait)
                self.handoff_ms.append((time.perf_counter() - packet.captured_at) * 1000)
                yield packet

        return generate()

    def report(self) -> str:
        line = (f"captured {self.captured_blocks * 10} ms @ {self.capture_rate} Hz"
                f"{f' (reopened {self.capture_restarts}x, gap-filled {self.gap_filled_ms} ms)' if self.capture_restarts or self.gap_filled_ms else ''}, "
                f"overflows {self.capture_overflows}, "
                f"RTP packets {self.packets_out}, queued {self.packet_q.qsize() if self.packet_q else 0}, "
                f"max encode {self.encode_max * 1000:.2f} ms/10ms, "
                f"input peak {20 * np.log10(self.peak + 1e-9):.1f} dBFS")
        if self.handoff_ms:
            h = np.array(self.handoff_ms)
            line += f", capture->L2CAP p50/p95/max {np.percentile(h, 50):.1f}/{np.percentile(h, 95):.1f}/{h.max():.1f} ms"
            self.handoff_ms.clear()
        self.encode_max = 0.0
        self.peak = 0.0
        return line
