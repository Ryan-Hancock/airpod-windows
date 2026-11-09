"""Stream a pre-encoded SBC or ADTS-AAC file to the AirPods over A2DP.

Usage (from WSL):
  tools/winpy.sh tools/a2dp_play.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF media/test.sbc [--volume=N] [--start-delay=S]
  tools/winpy.sh tools/a2dp_play.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF media/test.aac [--volume=N] [--start-delay=S]

--volume=N sets AVRCP absolute volume (0..127) before the stream starts. Without it the
AirPods' current volume is only read and reported (it was 0 for this host at first).
--start-delay=S waits S seconds (default 1.5) before AVDTP START so the AirPods'
"connected" chime doesn't overlap the audio.

The fixes this relies on (all verified, see README) live in airpodlink/:
event-trim HCI quirk, authenticate-before-encrypt, incoming-link race, sample-clock RTP
timestamps, correct SBC bitpool range, AVRCP absolute volume.
"""
import asyncio
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from airpodlink.a2dp_stream import FileStream  # noqa: E402
from airpodlink.link import connect_airpods, disconnect, open_device, say, wait_disconnected  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())


async def main(spec: str, address: str, audio_path: str, volume: int | None, start_delay: float) -> int:
    async with open_device(spec) as (device, avrcp_protocol):
        connection = await connect_airpods(device, address)
        stream = FileStream(connection, avrcp_protocol, audio_path, volume, start_delay)
        try:
            await stream.start()
            pump_done = asyncio.ensure_future(stream.done())
            dropped = asyncio.ensure_future(wait_disconnected(connection))
            await asyncio.wait([pump_done, dropped], return_when=asyncio.FIRST_COMPLETED)
            say("pump finished" if pump_done.done() else "stopped early")
            if not dropped.done():
                await asyncio.sleep(1.0)  # let the sink drain before we tear down
                await disconnect(connection)
        finally:
            await stream.close()
    return 0


if __name__ == "__main__":
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    vol = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--volume=")), None)
    if vol is not None and not 0 <= vol <= 127:
        sys.exit("--volume must be 0..127")
    delay = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--start-delay=")), 1.5)
    sys.exit(asyncio.run(main(*positional[:3], vol, delay)))
