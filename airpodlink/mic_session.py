"""AACP hi-res mic session: handshake, conversation awareness, START/STOP, stall watchdog,
and mute (stem click via AVRCP + global hotkey) wired to a MicPipeline."""

from __future__ import annotations

import asyncio
import contextlib
import time

from bumble.device import Connection

from airpodlink.aacp import AacpClient, Opcode, pause_conversation_awareness
from airpodlink.link import say
from airpodlink.mic_pipeline import MicPipeline
from airpodlink.mute import MuteControl

# A stall can mean the AirPods are leaving (e.g. Apple automatic switching to a nearby
# iPhone took them over: mic stops, then the link drops ~3 s later). Poking the mic early
# seemed to coincide with the drop once, so wait longer and only restart on a live link.
STALL_TIMEOUT = 4.0
START_ACK = bytes([0x00, 0x00, 0x03, 0x00])


class MicSession:
    def __init__(self, connection: Connection, avrcp_delegate, pipeline: MicPipeline,
                 start_muted: bool = False, hotkey: bool = True, mute: MuteControl | None = None) -> None:
        self.connection = connection
        self.link_up = True
        connection.on("disconnection", self._on_disconnection)
        self.pipeline = pipeline
        self.start_muted = start_muted
        self.live = False    # mic stream acknowledged and flowing (stem click = mute)
        self.wanted = False  # mic stream requested (START sent, not yet stopped)
        self.last_sdu = time.monotonic()
        self.restarts = 0
        self.restore_convo: bool | None = None
        # Pass a long-lived MuteControl to keep mute state/hotkey across reconnects.
        self.owns_mute = mute is None
        self.mute = mute or MuteControl(self._set_muted, lambda: pipeline.muted)
        if start_muted:
            pipeline.muted = True
        self.avrcp_delegate = avrcp_delegate
        avrcp_delegate.key_listeners.append(self._on_key)
        self.on_idle_key = None  # key handler while the mic is off (e.g. Windows media keys)
        self.control_listeners: list = []  # (opcode, data) for every AACP control packet
        self.aacp = AacpClient(connection, on_audio_sdu=self._on_sdu, on_control_packet=self._on_control)
        if hotkey and self.owns_mute:
            ok = self.mute.start_hotkey(asyncio.get_running_loop())
            say(f"mute hotkey Ctrl+Alt+M: {'registered' if ok else 'unavailable'}")

    def _set_muted(self, muted: bool) -> None:
        self.pipeline.muted = muted

    def _on_disconnection(self, _reason) -> None:
        self.link_up = False
        self.live = False
        self.pipeline.live = False
        self.pipeline.reset()

    def _on_key(self, key, pressed: bool) -> None:
        if self.live:
            self.mute.on_avrcp_key(key, pressed)
        elif self.on_idle_key:
            self.on_idle_key(key, pressed)

    def _on_sdu(self, sdu: bytes) -> None:
        self.last_sdu = time.monotonic()
        self.pipeline.feed_sdu(sdu)

    def _on_control(self, opcode: int, data: bytes) -> None:
        for listener in self.control_listeners:
            listener(opcode, data)
        if opcode == Opcode.EAR_DETECTION and len(data) >= 2:
            worn = 0x00 in data[:2]  # 0x00 = in-ear (librepods), 1 out, 2 in-case, 3 disconnected
            if worn != self.pipeline.worn:
                self.pipeline.worn = worn
                if not worn:
                    self.pipeline.reset()
                say(f"mic: {'worn -> live' if worn else 'no bud in ear -> silenced'}")
        if opcode == Opcode.MIC_STREAM and data[:4] == START_ACK:
            self.live = True
            self.pipeline.live = True
            say(f"mic START acknowledged -> {'live (MUTED)' if self.pipeline.muted else 'live'}")

    async def start(self, mic: bool = True) -> None:
        """Open AACP (kept for the whole connection: ear detection, settings). The mic stream
        itself starts now only if `mic`; otherwise call mic_on()/mic_off() on demand."""
        await self.aacp.open()
        await self.aacp.handshake()
        await asyncio.sleep(1.5)  # initial status burst (incl. conversation awareness if A2DP is up)
        if mic:
            await self.mic_on()

    async def mic_on(self) -> None:
        if self.wanted or not self.link_up:
            return
        self.wanted = True
        self.restarts = 0
        self.restore_convo = pause_conversation_awareness(self.aacp)
        self.pipeline.reset()
        self.last_sdu = time.monotonic()
        self.aacp.mic_start()

    async def mic_off(self) -> None:
        if not self.wanted:
            return
        self.wanted = False
        self.live = False
        self.pipeline.live = False
        if not self.link_up:
            return
        with contextlib.suppress(Exception):
            self.aacp.mic_stop()
        say("AACP: mic stream stopped (no recorder)")
        if self.restore_convo is not None:
            with contextlib.suppress(Exception):
                self.aacp.set_conversation_detection(self.restore_convo)
            self.restore_convo = None

    async def watchdog(self) -> None:
        """Call about once a second: restarts the mic stream if it stalls on a live link."""
        if not self.link_up or not self.wanted:
            return
        if time.monotonic() - self.last_sdu > STALL_TIMEOUT and self.restarts < 2:
            self.restarts += 1
            say(f"MIC: no audio for >{STALL_TIMEOUT:.0f}s -> restart (STOP+START)")
            self.aacp.mic_stop()
            await asyncio.sleep(0.3)
            self.pipeline.reset()
            self.aacp.mic_start()
            self.last_sdu = time.monotonic()

    async def stop(self) -> None:
        with contextlib.suppress(ValueError):
            self.avrcp_delegate.key_listeners.remove(self._on_key)
        await self.mic_off()
        if self.link_up:
            await asyncio.sleep(0.5)
            await self.aacp.close()
