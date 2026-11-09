"""A2DP streaming to the AirPods plus AVRCP absolute volume.

A stream takes a *media source*: anything with
    codec_type: int                                   (A2DP_SBC_CODEC_TYPE / A2DP_MPEG_2_4_AAC_CODEC_TYPE)
    async capabilities() -> MediaCodecCapabilities    (what we will configure the sink with)
    packets(mtu) -> async iterator of MediaPacket     (with .timestamp_seconds for pacing)
    async start() / async stop()                      (optional, e.g. open capture device)
FileSource (pre-encoded SBC/ADTS files) lives here; LiveSource (Windows capture + live
encode) is in airpodlink.live_source.
"""

from __future__ import annotations

import asyncio
import contextlib

from bumble import avc, avrcp
from bumble.a2dp import A2DP_MPEG_2_4_AAC_CODEC_TYPE, A2DP_SBC_CODEC_TYPE, SbcMediaCodecInformation
from bumble.apps.player.player import aac_codec_capabilities, sbc_codec_capabilities
from bumble.avdtp import (
    AVDTP_AUDIO_MEDIA_TYPE,
    AVDTP_DELAY_REPORTING_SERVICE_CATEGORY,
    MediaCodecCapabilities,
    MediaPacketPump,
    find_avdtp_service_with_connection,
)
from bumble.avdtp import Protocol as AvdtpProtocol
from bumble.device import Connection

from airpodlink.a2dp_sources import SampleClockAacPacketSource, SampleClockSbcPacketSource
from airpodlink.link import say


async def set_absolute_volume(protocol: avrcp.Protocol, volume: int) -> None:
    await asyncio.wait_for(protocol.send_avrcp_command(
        avc.CommandFrame.CommandType.CONTROL, avrcp.SetAbsoluteVolumeCommand(max(0, min(127, volume)))), 3)


async def avrcp_volume(protocol: avrcp.Protocol, connection: Connection, volume,
                       ready: asyncio.Event, on_remote_change=None, state: dict | None = None) -> None:
    """Open AVRCP, report the AirPods' absolute volume, optionally set it.

    `volume`: int, a zero-arg callable returning int|None (evaluated at connect, e.g. the
    current Windows slider), or None to leave the AirPods' volume alone. (The AirPods start
    a never-seen host at 0/127.) Sets `ready` once handled (or AVRCP failed), then reports
    volume changes made on the AirPods to `on_remote_change(v)`.
    """
    try:
        await asyncio.wait_for(protocol.connect(connection), 5)
        if state is not None:
            state["avrcp"] = True
        monitor = protocol.monitor_volume()
        current = await asyncio.wait_for(monitor.__anext__(), 3)
        say(f"AVRCP: AirPods absolute volume {current}/127 ({current * 100 // 127}%)")
        target = volume() if callable(volume) else volume
        if target is not None and target != current:
            await set_absolute_volume(protocol, target)
            say(f"AVRCP: set absolute volume -> {target}/127")
        ready.set()
        async for v in monitor:
            say(f"AVRCP: AirPods volume changed: {v}/127")
            if on_remote_change:
                on_remote_change(v)
    except Exception as error:  # noqa: BLE001 - diagnostics only, never break the stream
        say(f"AVRCP: {error!r}")
    finally:
        ready.set()


def clamp_sbc_to_sink(caps: MediaCodecCapabilities, sink) -> MediaCodecCapabilities:
    """Use the sink's SBC bitpool range, rebuilding so the serialized bytes match.

    (Bumble caches the original bytes otherwise and sends a stale range; bumble-player also
    hard-codes max bitpool 40, above the AirPods' 27.)
    """
    for c in sink.capabilities:
        if isinstance(c, MediaCodecCapabilities) and isinstance(
            c.media_codec_information, SbcMediaCodecInformation
        ) and isinstance(caps.media_codec_information, SbcMediaCodecInformation):
            info = caps.media_codec_information
            info.minimum_bitpool_value = c.media_codec_information.minimum_bitpool_value
            info.maximum_bitpool_value = c.media_codec_information.maximum_bitpool_value
            return MediaCodecCapabilities(media_type=caps.media_type,
                                          media_codec_type=caps.media_codec_type,
                                          media_codec_information=info)
    return caps


class FileSource:
    """Pre-encoded raw SBC or ADTS-AAC file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.format = "aac" if path.endswith((".aac", ".adts")) else "sbc"
        self.codec_type = A2DP_MPEG_2_4_AAC_CODEC_TYPE if self.format == "aac" else A2DP_SBC_CODEC_TYPE
        self._file = None

    async def _read(self, n: int) -> bytes:
        return self._file.read(n)

    async def start(self) -> None:
        self._file = open(self.path, "rb")  # noqa: SIM115 - closed in stop()

    async def capabilities(self) -> MediaCodecCapabilities:
        caps = await (sbc_codec_capabilities if self.format == "sbc" else aac_codec_capabilities)(self._read)
        self._file.seek(0)
        return caps

    def packets(self, mtu: int):
        cls = SampleClockSbcPacketSource if self.format == "sbc" else SampleClockAacPacketSource
        return cls(self._read, mtu).packets

    async def stop(self) -> None:
        if self._file:
            self._file.close()

    def __str__(self) -> str:
        return f"{self.format.upper()} file {self.path}"


class A2dpStream:
    """One A2DP stream from a media source. `await start()`, then `await done()`."""

    def __init__(self, connection: Connection, avrcp_protocol: avrcp.Protocol, source,
                 volume=None, start_delay: float = 1.5, on_remote_volume=None) -> None:
        self.on_remote_volume = on_remote_volume
        self.avrcp_state: dict = {"avrcp": False}
        self.connection = connection
        self.avrcp_protocol = avrcp_protocol
        self.source = source
        self.volume = volume
        self.start_delay = start_delay
        self.pump: MediaPacketPump | None = None
        self.stream = None
        self.volume_task: asyncio.Task | None = None

    async def start(self) -> None:
        version = await find_avdtp_service_with_connection(self.connection)
        avdtp = await AvdtpProtocol.connect(self.connection, version)
        await avdtp.discover_remote_endpoints()
        await self.source.start()

        sink = avdtp.find_remote_sink_by_codec(AVDTP_AUDIO_MEDIA_TYPE, self.source.codec_type,
                                               getattr(self.source, "vendor_id", 0),
                                               getattr(self.source, "vendor_codec_id", 0))
        if sink is None:
            raise RuntimeError(f"no sink on the AirPods for {self.source}")
        delay_reporting = any(
            c.service_category == AVDTP_DELAY_REPORTING_SERVICE_CATEGORY for c in sink.capabilities
        )
        caps = clamp_sbc_to_sink(await self.source.capabilities(), sink)

        self.pump = MediaPacketPump(self.source.packets(avdtp.l2cap_channel.peer_mtu))
        local = avdtp.add_source(caps, self.pump, delay_reporting)
        local.on("delay_report", lambda d: say(f"A2DP: delay report {d / 10:.1f} ms"))
        for event in ("suspend", "close", "abort"):
            local.on(event, lambda *a, e=event: say(f"A2DP: stream event {e.upper()}"))
        stream = await avdtp.create_stream(local, sink)
        self.stream = stream  # exposes rtp_channel (e.g. for flushable media)

        # Set the volume before audio starts, then let the AirPods' "connected" chime finish.
        volume_ready = asyncio.Event()
        self.volume_task = asyncio.ensure_future(
            avrcp_volume(self.avrcp_protocol, self.connection, self.volume, volume_ready,
                         self.on_remote_volume, self.avrcp_state))
        try:
            await asyncio.wait_for(volume_ready.wait(), 8)
        except asyncio.TimeoutError:
            say("AVRCP volume step timed out; starting anyway")
        await asyncio.sleep(self.start_delay)
        await stream.start()
        say(f"A2DP: streaming {self.source} to sink seid={sink.seid}")

    async def set_volume(self, volume: int) -> bool:
        """Set the AirPods' absolute volume (0..127) now; False if AVRCP isn't up."""
        if not self.avrcp_state.get("avrcp"):
            return False
        await set_absolute_volume(self.avrcp_protocol, volume)
        return True

    async def done(self) -> None:
        assert self.pump is not None
        await self.pump.wait_for_completion()

    async def close(self) -> None:
        """Stop the pump (if still running) before stopping the source it reads from."""
        if self.volume_task:
            self.volume_task.cancel()
        if self.pump is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.pump.stop()
        with contextlib.suppress(Exception):
            await self.source.stop()


def FileStream(connection, avrcp_protocol, path, volume=None, start_delay=1.5) -> A2dpStream:  # noqa: N802
    """Back-compat helper used by the phase 1-3 tools."""
    return A2dpStream(connection, avrcp_protocol, FileSource(path), volume, start_delay)
