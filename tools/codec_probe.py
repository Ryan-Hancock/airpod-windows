"""Ask the AirPods how much they buffer for each A2DP codec, without streaming audio.

For each sink endpoint (SBC, AAC-LC, Apple AAC-ELD vendor codec 0x004C:0x8001) this does
AVDTP SET_CONFIGURATION + OPEN with a local source endpoint that supports delay reporting,
records the AirPods' DELAYREPORT (their playback buffer, in 1/10 ms), then CLOSEs.

Usage: tools/winpy.sh tools/codec_probe.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bumble.a2dp import (  # noqa: E402
    A2DP_MPEG_2_4_AAC_CODEC_TYPE,
    A2DP_NON_A2DP_CODEC_TYPE,
    A2DP_SBC_CODEC_TYPE,
    VendorSpecificMediaCodecInformation,
)
from bumble.avdtp import AVDTP_AUDIO_MEDIA_TYPE, MediaCodecCapabilities, find_avdtp_service_with_connection  # noqa: E402
from bumble.avdtp import Protocol as AvdtpProtocol  # noqa: E402

from airpodlink.a2dp_stream import clamp_sbc_to_sink  # noqa: E402
from airpodlink.link import connect_airpods, disconnect, open_device, say  # noqa: E402
from airpodlink.live_source import LiveSource  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())

APPLE = 0x004C
AAC_ELD = 0x8001
# SET_CONFIGURATION value used by macOS per han-um's Pico port (aac-eld-apple.md):
# object type 0x0080 (AAC-ELD), 48 kHz (0x008) | stereo (0x4), VBR=1, max bitrate 256000.
ELD_CONFIG = bytes([0x00, 0x80, 0x00, 0x84, 0x00, 0x83, 0xE8, 0x00])


async def probe(avdtp, name, caps, codec_type, vendor_id=0, codec_id=0):
    sink = avdtp.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, codec_type, vendor_id, codec_id)
    if sink is None:
        say(f"{name}: no matching sink")
        return None
    caps = clamp_sbc_to_sink(caps, sink)
    reports = []
    source = avdtp.add_source(caps, None, True)
    source.on("delay_report", lambda d: reports.append(d / 10))
    try:
        stream = await asyncio.wait_for(avdtp.create_stream(source, sink), 5)
    except Exception as error:  # noqa: BLE001 - report and continue with the next codec
        say(f"{name}: configure/open REJECTED: {error!r}")
        return None
    await asyncio.sleep(1.0)
    say(f"{name} (sink seid {sink.seid}): delay report(s) {reports or 'none'} ms")
    with contextlib.suppress(Exception):
        await asyncio.wait_for(stream.close(), 3)
    await asyncio.sleep(0.5)
    return reports


async def main(spec, address):
    eld = MediaCodecCapabilities(
        media_type=AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=A2DP_NON_A2DP_CODEC_TYPE,
        media_codec_information=VendorSpecificMediaCodecInformation(APPLE, AAC_ELD, ELD_CONFIG))
    plan = [
        ("Apple AAC-ELD", eld, A2DP_NON_A2DP_CODEC_TYPE, APPLE, AAC_ELD),
        ("AAC-LC", await LiveSource("aac").capabilities(), A2DP_MPEG_2_4_AAC_CODEC_TYPE, 0, 0),
        ("SBC", await LiveSource("sbc").capabilities(), A2DP_SBC_CODEC_TYPE, 0, 0),
    ]
    async with open_device(spec) as (device, _avrcp):
        # One fresh connection per codec: the AirPods reject a second SET_CONFIGURATION on
        # the same signalling session after a CLOSE (observed: ProtocolError).
        for name, caps, codec_type, vendor_id, codec_id in plan:
            connection = await connect_airpods(device, address)
            version = await find_avdtp_service_with_connection(connection)
            avdtp = await AvdtpProtocol.connect(connection, version)
            await avdtp.discover_remote_endpoints()
            await probe(avdtp, name, caps, codec_type, vendor_id, codec_id)
            await disconnect(connection)
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
