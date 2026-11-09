"""Phase 3: AirPods hi-res mic -> VB-Cable, so Windows apps can use "CABLE Output" as a mic.

Usage (from WSL or a Windows terminal):
  tools/winpy.sh tools/mic_bridge.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--seconds=N]
        [--play=media/test.aac] [--volume=40] [--gain-db=0] [--verify] [--start-muted]

  --seconds=N     run for N seconds (default: until 'q' + Enter or Ctrl+C)
  --play=FILE     also stream FILE over A2DP (keeps the AirPods reporting their settings)
  --gain-db=G     fixed gain applied to the decoded mic (the raw stream peaks around -28 dBFS)
  --verify        record "CABLE Output" (what a Windows app would hear) to captures/bridge-*.wav
  --start-muted   stay muted until you type 'm' + Enter

Mute toggles: a short AirPods stem click (arrives as AVRCP PAUSE/PLAY while the mic is
live), the global hotkey Ctrl+Alt+M, or 'm' + Enter in the console. 'q' + Enter quits.

Mute/privacy: output is silence until the AirPods acknowledge mic START, whenever muted,
on any stall/underrun, and on shutdown.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
import threading
import time
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from airpodlink.a2dp_stream import FileStream  # noqa: E402
from airpodlink.link import ROOT, connect_airpods, disconnect, open_device, say  # noqa: E402
from airpodlink.mic_pipeline import OUT_RATE, MicPipeline  # noqa: E402
from airpodlink.mic_session import MicSession  # noqa: E402
from airpodlink.mute import MuteControl  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())


class CableRecorder:
    """Records the WASAPI "CABLE Output" capture endpoint, i.e. what Windows apps receive."""

    def __init__(self, path: pathlib.Path) -> None:
        wasapi = next(i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"])
        from airpodlink.devices import MIC, pick, wasapi_names

        name = pick(MIC, wasapi_names("input"))
        index = next(i for i, d in enumerate(sd.query_devices())
                     if d["hostapi"] == wasapi and d["max_input_channels"] > 0 and d["name"] == name)
        self.path = path
        self.chunks: list[np.ndarray] = []
        self.stream = sd.InputStream(device=index, samplerate=OUT_RATE, channels=1, dtype="float32",
                                     callback=lambda d, *_: self.chunks.append(d[:, 0].copy()))

    def __enter__(self):
        self.stream.start()
        return self

    def __exit__(self, *exc):
        self.stream.stop()
        self.stream.close()
        pcm = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
        with wave.open(str(self.path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(OUT_RATE)
            w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        say(f"verify: recorded {pcm.size / OUT_RATE:.1f} s from CABLE Output, "
            f"peak {20 * np.log10(peak + 1e-9):.1f} dBFS -> {self.path}")


def console_thread(loop: asyncio.AbstractEventLoop, mute: MuteControl, quit_event: asyncio.Event):
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "m":
            loop.call_soon_threadsafe(mute.toggle, "console")
        elif cmd == "q":
            loop.call_soon_threadsafe(quit_event.set)
            return


async def main(spec, address, seconds, play, volume, gain_db, verify, start_muted) -> int:
    if play and not pathlib.Path(play).is_absolute():
        play = str(ROOT / play)
    pipeline = MicPipeline(gain_db=gain_db)
    say(f"output: {pipeline.start()}")
    quit_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    recorder = CableRecorder(ROOT / "captures" / time.strftime("bridge-%Y%m%d-%H%M%S.wav")) if verify else None
    try:
        async with open_device(spec) as (device, avrcp_protocol):
            connection = await connect_airpods(device, address)
            connection.on("disconnection", lambda _r: loop.call_soon_threadsafe(quit_event.set))
            stream = None
            if play:
                stream = FileStream(connection, avrcp_protocol, play, volume)
                await stream.start()
            mic = MicSession(connection, avrcp_protocol.delegate, pipeline, start_muted)
            if sys.stdin and sys.stdin.isatty():
                threading.Thread(target=console_thread, args=(loop, mic.mute, quit_event), daemon=True).start()
            with recorder or contextlib.nullcontext():
                try:
                    await mic.start()
                    started = time.monotonic()
                    while not quit_event.is_set() and (seconds is None or time.monotonic() - started < seconds):
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(quit_event.wait(), 1.0)
                        say(f"MIC: {pipeline.report()}")
                        await mic.watchdog()
                finally:
                    await mic.stop()
            if stream:
                await stream.close()
            await disconnect(connection)
    finally:
        pipeline.stop()
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main(
            args[0], args[1],
            seconds=float(opts["seconds"]) if "seconds" in opts else None,
            play=opts.get("play"),
            volume=int(opts.get("volume", 40)),
            gain_db=float(opts.get("gain-db", 0)),
            verify="--verify" in sys.argv,
            start_muted="--start-muted" in sys.argv,
        )))
