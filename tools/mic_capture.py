"""Phase 2: start the AirPods' AACP hi-res mic stream, log it, and save the raw packets.

Usage (from WSL):
  tools/winpy.sh tools/mic_capture.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--seconds=20]
        [--play=media/test.sbc] [--volume=40] [--out=captures] [--probe] [--burst-handshake]

--probe: AACP handshake only, no mic; log control traffic for --seconds.
--burst-handshake: send the three init packets back to back (Pico port) instead of
waiting for the handshake reply (librepods order, default).

Sequence: connect -> (optional A2DP playback) -> AACP channel + handshake -> turn off
conversation awareness if it is on -> mic START -> capture -> mic STOP -> restore
conversation awareness -> disconnect.

Output (captures/mic-<time>.*):
  .sdu  raw 0x58 audio SDUs:  [rx_time_ns u64le][len u16le][sdu]   (everything, for re-parsing)
  .au   demuxed AAC-ELD AUs:  [timestamp u32le][len u16le][au]    (input for phase 3 decode)
  .log  per-second stats are printed to stdout only.

The SDU callback runs on the asyncio event loop; it only timestamps and appends bytes.
That is fine for capture, but decode/output (phase 3) must not run there.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from airpodlink.a2dp_stream import FileStream  # noqa: E402
from airpodlink.aacp import AacpClient, demux_audio_sdu, pause_conversation_awareness  # noqa: E402
from airpodlink.link import ROOT, connect_airpods, disconnect, open_device, say  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
STALL_TIMEOUT = 2.0


class MicStats:
    def __init__(self, sdu_file, au_file):
        self.sdu_file = sdu_file
        self.au_file = au_file
        self.reset_interval()
        self.total_sdus = 0
        self.total_aus = 0
        self.truncated = 0
        self.first_rx = None
        self.last_rx = None
        self.last_au_ts = None
        self.ts_deltas: dict[int, int] = {}
        self.max_rx_gap = 0.0

    def reset_interval(self):
        self.sdus = 0
        self.aus = 0
        self.au_bytes = 0
        self.au_min = 1 << 30
        self.au_max = 0
        self.sdu_max = 0
        self.aus_per_sdu: set[int] = set()

    def on_sdu(self, sdu: bytes) -> None:
        now = time.monotonic()
        if self.last_rx is not None:
            self.max_rx_gap = max(self.max_rx_gap, now - self.last_rx)
        self.first_rx = self.first_rx or now
        self.last_rx = now
        self.sdu_file.write(struct.pack("<QH", time.monotonic_ns(), len(sdu)) + sdu)
        aus, truncated = demux_audio_sdu(sdu)
        self.truncated += truncated
        self.sdus += 1
        self.total_sdus += 1
        self.sdu_max = max(self.sdu_max, len(sdu))
        self.aus_per_sdu.add(len(aus))
        for ts, au in aus:
            self.au_file.write(struct.pack("<IH", ts, len(au)) + au)
            if self.last_au_ts is not None:
                d = (ts - self.last_au_ts) & 0xFFFFFFFF
                self.ts_deltas[d] = self.ts_deltas.get(d, 0) + 1
            self.last_au_ts = ts
            self.aus += 1
            self.total_aus += 1
            self.au_bytes += len(au)
            self.au_min = min(self.au_min, len(au))
            self.au_max = max(self.au_max, len(au))

    def report(self) -> str:
        avg = self.au_bytes // self.aus if self.aus else 0
        line = (f"MIC: {self.sdus} SDU/s, {self.aus} AU/s, AU bytes min/avg/max "
                f"{self.au_min if self.aus else 0}/{avg}/{self.au_max}, "
                f"~{self.au_bytes * 8 / 1000:.0f} kbit/s, AUs/SDU {sorted(self.aus_per_sdu)}, "
                f"SDU max {self.sdu_max} B")
        self.reset_interval()
        return line


async def main(spec, address, seconds, play, volume, out_dir, probe=False, staggered=True) -> int:
    out_dir = pathlib.Path(out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(exist_ok=True)
    stem = out_dir / time.strftime("mic-%Y%m%d-%H%M%S")

    async with open_device(spec) as (device, avrcp_protocol):
        connection = await connect_airpods(device, address)
        stream = None
        if play and not pathlib.Path(play).is_absolute():
            play = str(ROOT / play)  # Windows python runs from C:\, resolve against the repo
        if play:
            stream = FileStream(connection, avrcp_protocol, play, volume)
            await stream.start()

        with open(f"{stem}.sdu", "wb") as sdu_file, open(f"{stem}.au", "wb") as au_file:
            stats = MicStats(sdu_file, au_file)
            aacp = AacpClient(connection, on_audio_sdu=stats.on_sdu)
            await aacp.open()
            await aacp.handshake(staggered=staggered)
            await asyncio.sleep(1.5)  # collect the initial burst of control/status packets
            if probe:
                await asyncio.sleep(seconds)
                say(f"known controls: { {hex(k): v.hex() for k, v in aacp.controls.items()} }")
                await aacp.close()
                await disconnect(connection)
                return 0

            restore_convo = pause_conversation_awareness(aacp)

            try:
                aacp.mic_start()
                started = time.monotonic()
                restarts = 0
                while time.monotonic() - started < seconds:
                    await asyncio.sleep(1.0)
                    say(stats.report())
                    last = stats.last_rx or started
                    if time.monotonic() - last > STALL_TIMEOUT and restarts < 2:
                        restarts += 1
                        say(f"MIC: no audio for >{STALL_TIMEOUT:.0f}s, restarting stream (STOP+START)")
                        aacp.mic_stop()
                        await asyncio.sleep(0.3)
                        aacp.mic_start()
                        stats.last_rx = time.monotonic()
            finally:
                with contextlib.suppress(Exception):
                    aacp.mic_stop()
                if restore_convo is not None:
                    with contextlib.suppress(Exception):
                        aacp.set_conversation_detection(restore_convo)
                        say("conversation awareness restored to ON")
                await asyncio.sleep(1.0)

            span = (stats.last_rx - stats.first_rx) if stats.first_rx and stats.last_rx else 0
            say(f"TOTAL: {stats.total_sdus} SDUs, {stats.total_aus} AUs over {span:.1f}s "
                f"({stats.total_aus / span if span else 0:.1f} AU/s), truncated SDUs {stats.truncated}, "
                f"max SDU gap {stats.max_rx_gap * 1000:.0f} ms")
            top = sorted(stats.ts_deltas.items(), key=lambda kv: -kv[1])[:5]
            say(f"AU timestamp deltas (value: count): {top}")
            await aacp.close()

        if stream:
            await stream.close()
        await disconnect(connection)
    say(f"saved {stem}.sdu and {stem}.au")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    sys.exit(asyncio.run(main(
        args[0], args[1],
        seconds=float(opts.get("seconds", 20)),
        play=opts.get("play"),
        volume=int(opts["volume"]) if "volume" in opts else 40,
        out_dir=opts.get("out", "captures"),
        probe="--probe" in sys.argv,
        staggered="--burst-handshake" not in sys.argv,
    )))
