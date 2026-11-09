"""A2DP RTP packet sources with spec-correct timestamps.

Bumble's SbcPacketSource / AacPacketSource (bumble 0.0.235) put the RTP timestamp in
milliseconds (`int(1000 * seconds)`). A2DP (and RFC 3551 / RFC 3016 for AAC) clock the
media RTP timestamp at the audio sampling rate, so a packet of 15 SBC frames should
advance it by 1920, not 43. These sources are drop-in replacements that use a
sample clock. `timestamp_seconds` (used by MediaPacketPump for pacing) is unchanged.

The SBC source also flushes the final partial packet, which Bumble's version drops.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from bumble.a2dp import SBC_MAX_FRAMES_IN_RTP_PAYLOAD, AacParser, SbcParser
from bumble.codecs import AacAudioRtpPacket
from bumble.rtp import MediaPacket

RTP_PAYLOAD_TYPE = 96
RTP_HEADER_SIZE = 12


def _media_packet(sequence_number: int, sample_count: int, sampling_frequency: int, payload: bytes):
    packet = MediaPacket(
        2, 0, 0, 0,
        sequence_number & 0xFFFF,
        sample_count & 0xFFFFFFFF,  # RTP clock = sampling rate
        0, [], RTP_PAYLOAD_TYPE, payload,
    )
    packet.timestamp_seconds = sample_count / sampling_frequency
    return packet


class SampleClockSbcPacketSource:
    def __init__(self, read: Callable[[int], Awaitable[bytes]], mtu: int) -> None:
        self.read = read
        self.mtu = mtu

    @property
    def packets(self):
        async def generate_packets():
            sequence_number = 0
            sample_count = 0
            frames = []
            frames_size = 0
            sampling_frequency = 0
            # 1 byte SBC media payload header (frame count) after the RTP header.
            max_rtp_payload = self.mtu - RTP_HEADER_SIZE - 1

            def flush():
                payload = bytes([len(frames) & 0x0F]) + b"".join(f.payload for f in frames)
                return _media_packet(sequence_number, sample_count, sampling_frequency, payload)

            async for frame in SbcParser(self.read).frames:
                sampling_frequency = frame.sampling_frequency
                if frames and (
                    frames_size + len(frame.payload) > max_rtp_payload
                    or len(frames) == SBC_MAX_FRAMES_IN_RTP_PAYLOAD
                ):
                    yield flush()
                    sequence_number += 1
                    sample_count += sum(f.sample_count for f in frames)
                    frames = []
                    frames_size = 0
                frames.append(frame)
                frames_size += len(frame.payload)
            if frames:
                yield flush()

        return generate_packets()


class SampleClockAacPacketSource:
    def __init__(self, read: Callable[[int], Awaitable[bytes]], mtu: int) -> None:
        self.read = read
        self.mtu = mtu

    @property
    def packets(self):
        async def generate_packets():
            sequence_number = 0
            sample_count = 0
            async for frame in AacParser(self.read).frames:
                payload = bytes(
                    AacAudioRtpPacket.for_simple_aac(
                        frame.sampling_frequency, frame.channel_configuration, frame.payload
                    )
                )
                yield _media_packet(sequence_number, sample_count, frame.sampling_frequency, payload)
                sequence_number += 1
                sample_count += frame.sample_count

        return generate_packets()
