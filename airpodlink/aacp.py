"""Apple Accessory Communication Protocol (AACP) client: control channel + hi-res mic.

Port of librepods PR #655 (linux-rust/src/bluetooth/aacp.rs, aacp_audio.rs); the Pico 2 W
port (han-um/USBPods-Pico2W-2-way-audio-implement, btstack_aacp.c) uses identical bytes.

Wire format, as reverse-engineered there (not verified against any Apple spec):
  - L2CAP basic-mode channel on PSM 0x1001.
  - Handshake packet is sent raw; every other packet is `04 00 04 00` + u16le opcode + data.
  - Control command (opcode 0x09): identifier byte + 4 value bytes.
  - Opcode 0x58 is the mic stream. START/STOP are fixed packets. Audio arrives as 0x58 SDUs
    with u16le subtype 0x0001 at offset 6: a 22-byte header, then N x
    [timestamp u32le][au_len u8][AAC-ELD access unit] (AAC-ELD mono, 64 kHz per the PR).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import struct
from collections.abc import Callable

from bumble import l2cap
from bumble.device import Connection

from airpodlink.link import say

AACP_PSM = 0x1001
# 0x58 audio SDUs can exceed 1 KB (PR #655 uses a 4 KB receive buffer).
AACP_MTU = 4096
HEADER = bytes([0x04, 0x00, 0x04, 0x00])

HANDSHAKE = bytes([0x00, 0x00, 0x04, 0x00, 0x01, 0x00, 0x02, 0x00,
                   0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
# 0xFF: the PR's "adaptive volume is actually useful" variant (librepods default was 0xD7).
SET_FEATURE_FLAGS = HEADER + bytes([0x4D, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
REQUEST_NOTIFICATIONS = HEADER + bytes([0x0F, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])
# librepods `send_some_packet` (opcode 0x29, 8 x 0xFF), sent right after the notification
# request. Purpose undocumented; included to match librepods' init sequence.
UNKNOWN_0x29 = HEADER + bytes([0x29, 0x00] + [0xFF] * 8)
MIC_START = HEADER + bytes([0x58, 0x00, 0x00, 0x00, 0x09, 0x00, 0x00, 0x01, 0x82,
                            0x00, 0x00, 0x00, 0x04, 0x96, 0x00])
MIC_STOP = HEADER + bytes([0x58, 0x00, 0x00, 0x00, 0x02, 0x00, 0x03, 0x01])

AUDIO_SDU_HEADER_LEN = 22


class Opcode(enum.IntEnum):
    BATTERY_INFO = 0x04
    EAR_DETECTION = 0x06
    CONTROL_COMMAND = 0x09
    TIPI_3 = 0x0C
    AUDIO_SOURCE = 0x0E
    REQUEST_NOTIFICATIONS = 0x0F
    SMART_ROUTING = 0x10
    SMART_ROUTING_RESP = 0x11
    SEND_CONNECTED_MAC = 0x14
    HEADTRACKING = 0x17
    STEM_PRESS = 0x19
    RENAME = 0x1A
    INFORMATION = 0x1D
    CONNECTED_DEVICES = 0x2E
    PROXIMITY_KEYS_REQ = 0x30
    PROXIMITY_KEYS_RSP = 0x31
    CONVERSATION_AWARENESS = 0x4B
    SET_FEATURE_FLAGS = 0x4D
    EQ_DATA = 0x53
    MIC_STREAM = 0x58


class Control(enum.IntEnum):
    MIC_MODE = 0x01
    BUTTON_SEND_MODE = 0x05
    OWNS_CONNECTION = 0x06
    EAR_DETECTION_CONFIG = 0x0A
    LISTENING_MODE = 0x0D
    VOICE_TRIGGER = 0x12
    SINGLE_CLICK_MODE = 0x14
    DOUBLE_CLICK_MODE = 0x15
    CLICK_HOLD_MODE = 0x16
    DOUBLE_CLICK_INTERVAL = 0x17
    CLICK_HOLD_INTERVAL = 0x18
    LISTENING_MODE_CONFIGS = 0x1A
    ONE_BUD_ANC_MODE = 0x1B
    CROWN_ROTATION_DIRECTION = 0x1C
    AUTO_ANSWER_MODE = 0x1E
    CHIME_VOLUME = 0x1F
    AUTOMATIC_CONNECTION_CONFIG = 0x20
    VOLUME_SWIPE_INTERVAL = 0x23
    CALL_MANAGEMENT_CONFIG = 0x24
    VOLUME_SWIPE_MODE = 0x25
    ADAPTIVE_VOLUME_CONFIG = 0x26
    SOFTWARE_MUTE_CONFIG = 0x27
    CONVERSATION_DETECT_CONFIG = 0x28  # 0x01 = on, 0x02 = off
    SSL = 0x29
    HEARING_AID = 0x2C
    AUTO_ANC_STRENGTH = 0x2E
    HPS_GAIN_SWIPE = 0x2F
    HRM_STATE = 0x30
    IN_CASE_TONE_CONFIG = 0x31
    SIRI_MULTITONE_CONFIG = 0x32
    HEARING_ASSIST_CONFIG = 0x33
    ALLOW_OFF_OPTION = 0x34
    SLEEP_DETECTION_CONFIG = 0x35
    ALLOW_AUTO_CONNECT = 0x36
    STEM_CONFIG = 0x39


def is_audio_sdu(sdu: bytes) -> bool:
    return (len(sdu) >= 8 and sdu[0] == 0x04 and sdu[2] == 0x04
            and struct.unpack_from("<H", sdu, 4)[0] == Opcode.MIC_STREAM
            and struct.unpack_from("<H", sdu, 6)[0] == 0x0001)


def demux_audio_sdu(sdu: bytes) -> tuple[list[tuple[int, bytes]], bool]:
    """Split one 0x58 audio SDU into [(timestamp, AU)]. Returns (aus, truncated)."""
    aus = []
    off = AUDIO_SDU_HEADER_LEN
    while off + 5 <= len(sdu):
        timestamp = struct.unpack_from("<I", sdu, off)[0]
        au_len = sdu[off + 4]
        start, end = off + 5, off + 5 + au_len
        if end > len(sdu):
            return aus, True
        aus.append((timestamp, sdu[start:end]))
        off = end
    return aus, False


def _name(enum_cls, value: int) -> str:
    try:
        return enum_cls(value).name
    except ValueError:
        return f"0x{value:02X}"


class AacpClient:
    """AACP control channel. Audio SDUs go to `on_audio_sdu` (called on the event loop)."""

    def __init__(self, connection: Connection, on_audio_sdu: Callable[[bytes], None] | None = None,
                 on_control_packet: Callable[[int, bytes], None] | None = None) -> None:
        self.connection = connection
        self.on_audio_sdu = on_audio_sdu
        self.on_control_packet = on_control_packet
        self.channel: l2cap.ClassicChannel | None = None
        self.controls: dict[int, bytes] = {}  # last known 4-byte value per control id
        self.first_control = asyncio.Event()
        self.any_packet = asyncio.Event()
        self.control_changed = asyncio.Event()

    async def open(self) -> None:
        self.channel = await self.connection.create_l2cap_channel(
            l2cap.ClassicChannelSpec(psm=AACP_PSM, mtu=AACP_MTU))
        self.channel.sink = self._on_sdu
        self.channel.on("close", lambda: say("AACP: channel closed"))
        say(f"AACP: channel open (peer MTU {self.channel.peer_mtu})")

    def send(self, packet: bytes) -> None:
        assert self.channel is not None
        self.channel.write(packet)

    async def handshake(self, timeout: float = 5.0, staggered: bool = True) -> None:
        """Handshake, feature flags, notification request; wait for the first control packet.

        staggered=True follows librepods (devices/airpods.rs): handshake, 300 ms, feature
        flags, 300 ms, notification request, opcode-0x29 packet. False sends handshake,
        flags and notification request back to back (Pico port).
        Not sent: librepods' proximity-keys request (asks for the AirPods' IRK/ENC keys).
        """
        self.send(HANDSHAKE)
        if staggered:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.any_packet.wait(), 2.0)
            await asyncio.sleep(0.3)
        self.send(SET_FEATURE_FLAGS)
        if staggered:
            await asyncio.sleep(0.3)
        self.send(REQUEST_NOTIFICATIONS)
        if staggered:
            self.send(UNKNOWN_0x29)
        await asyncio.wait_for(self.first_control.wait(), timeout)
        say("AACP: control channel confirmed")

    def send_control(self, control: Control, value: bytes) -> None:
        self.send(HEADER + bytes([Opcode.CONTROL_COMMAND, 0x00, control])
                  + value.ljust(4, b"\x00")[:4])
        # AirPods don't echo some controls back (PR #655), so track what we sent.
        self.controls[control] = value.ljust(4, b"\x00")[:4]
        say(f"AACP: -> control {control.name} = {value.hex()}")

    def conversation_detection(self) -> bool | None:
        value = self.controls.get(Control.CONVERSATION_DETECT_CONFIG)
        return None if value is None else value[0] == 0x01

    def set_conversation_detection(self, enabled: bool) -> None:
        self.send_control(Control.CONVERSATION_DETECT_CONFIG, bytes([0x01 if enabled else 0x02]))

    def mic_start(self) -> None:
        self.send(MIC_START)
        say("AACP: -> mic START")

    def mic_stop(self) -> None:
        self.send(MIC_STOP)
        say("AACP: -> mic STOP")

    async def close(self) -> None:
        if self.channel is not None:
            try:
                await asyncio.wait_for(self.channel.disconnect(), 3)
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass

    # ------------------------------------------------------------------ receive
    def _on_sdu(self, sdu: bytes) -> None:
        self.any_packet.set()
        if is_audio_sdu(sdu):
            if self.on_audio_sdu:
                self.on_audio_sdu(sdu)
            return
        if len(sdu) < 6 or sdu[:4] != HEADER:
            say(f"AACP: <- non-standard packet ({len(sdu)} B): {sdu[:48].hex()}")
            return
        self.first_control.set()
        opcode = struct.unpack_from("<H", sdu, 4)[0]
        data = sdu[6:]
        if self.on_control_packet:
            self.on_control_packet(opcode, data)
        if opcode == Opcode.CONTROL_COMMAND and len(data) >= 5:
            control, value = data[0], bytes(data[1:5])
            old = self.controls.get(control)
            self.controls[control] = value
            self.control_changed.set()
            change = "" if old is None else f" (was {old.hex()})"
            say(f"AACP: <- control {_name(Control, control)} = {value.hex()}{change}")
        elif opcode == Opcode.BATTERY_INFO and len(data) >= 1:
            parts = []
            count = data[0]
            for i in range(count):
                b = data[1 + i * 5: 6 + i * 5]
                if len(b) == 5:
                    comp = {0x01: "single", 0x02: "R", 0x04: "L", 0x08: "case"}.get(b[0], hex(b[0]))
                    status = {0x01: "charging", 0x02: "", 0x04: "disconnected"}.get(b[3], hex(b[3]))
                    parts.append(f"{comp} {b[2]}% {status}".strip())
            say(f"AACP: <- battery: {', '.join(parts)}")
        elif opcode == Opcode.INFORMATION:
            # NUL-separated strings: name, model, manufacturer, serial numbers, versions...
            # Serial numbers are redacted; only the first three fields are shown.
            fields = [f.decode(errors="replace") for f in data.split(b"\x00") if f.isascii() and f]
            say(f"AACP: <- INFORMATION: {fields[1:4]} (+{max(len(fields) - 4, 0)} fields incl. serials, not shown)")
        elif opcode == Opcode.EAR_DETECTION:
            states = {0: "in-ear", 1: "out", 2: "in-case", 3: "disconnected"}
            say(f"AACP: <- ear detection: {[states.get(b, hex(b)) for b in data[:2]]}")
        else:
            snippet = data[:40].hex() + ("…" if len(data) > 40 else "")
            say(f"AACP: <- {_name(Opcode, opcode)} ({len(data)} B): {snippet}")


def pause_conversation_awareness(aacp: AacpClient) -> bool | None:
    """Turn conversation awareness off for the mic if it's on.

    Returns the value to restore (True) or None if nothing was changed. The AirPods only
    report this setting while A2DP is streaming; if unknown we leave it alone rather than
    risk flipping the user's setting.
    """
    state = aacp.conversation_detection()
    if state is True:
        aacp.set_conversation_detection(False)
        say("conversation awareness was ON -> OFF while the mic is active (will restore)")
        return True
    say("conversation awareness already OFF" if state is False
        else "conversation awareness state unknown (not reported without A2DP); leaving it unchanged")
    return None
