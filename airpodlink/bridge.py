"""The AirPods bridge as a controllable service: session loop + commands + status.

Session loop (one long-lived dongle, one connection at a time):
  connect (or adopt the AirPods' own reconnection) -> A2DP live stream (+ AVRCP volume)
  -> AACP control channel (+ on-demand hi-res mic) -> monitor until the link drops.
Released ("Disconnect" in the tray): no paging, and the dongle is made non-connectable, so the
AirPods (and e.g. a nearby iPhone) are free until "Connect".

Commands are thread-safe (the tray runs on its own thread): connect(), disconnect(),
set_codec(), set_mic_gain(), toggle_mute(), quit(). status() returns a snapshot dict.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from bumble import hci
from bumble.core import BaseBumbleError, CommandTimeoutError

from airpodlink.a2dp_stream import A2dpStream
from airpodlink.aacp import Opcode
from airpodlink.devices import PLAYBACK
from airpodlink.link import connect_airpods, disconnect, open_device, reset_dongle, say
from airpodlink.link_monitor import LinkMonitor, enable_flushable_media
from airpodlink.live_source import LiveSource
from airpodlink.mic_demand import MicDemandWatcher
from airpodlink.mic_pipeline import MicPipeline
from airpodlink.mic_session import MicSession
from airpodlink.mute import MuteControl, media_key_forwarder
from airpodlink.settings import Settings
from airpodlink.volume_sync import EchoGuard, WindowsVolume, scalar_to_abs

RETRY_MIN, RETRY_MAX = 5.0, 30.0  # back off while the AirPods are away (case, out of range)
CONNECT_TIMEOUT = 25.0            # a connect attempt once hung for 38 h with no timeout
CODECS = {"eld": "AAC-ELD (low latency)", "aac": "AAC", "sbc": "SBC"}
EAR_STATES = {0: "in ear", 1: "out", 2: "in case", 3: "disconnected"}
BATTERY_STATUS = {0x01: "charging", 0x02: "", 0x04: "disconnected"}


class ControllerHung(RuntimeError):
    """The dongle stopped answering HCI commands; reopen (and USB-reset) it."""


async def _wait_any(*aws, timeout=None) -> None:
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


class Bridge:
    def __init__(self, spec: str, address: str, settings: Settings | None = None, *, mic: bool = True,
                 mic_mode: str = "auto", capture: str | None = None, stats_every: int = 1,
                 link_every: int = 2, aac_encoder: str = "fdk", seconds: float | None = None) -> None:
        self.spec, self.address = spec, address
        self.settings = settings or Settings.load()
        self.mic_enabled, self.mic_mode, self.capture = mic, mic_mode, capture
        self.stats_every, self.link_every = stats_every, link_every
        self.aac_encoder = aac_encoder
        self.deadline = time.monotonic() + seconds if seconds else None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.listeners: list[Callable[[], None]] = []
        self.state = "starting"
        self.detail = ""
        self.battery: dict[str, str] = {}
        self.ears: tuple[str, str] = ("?", "?")
        self.recorders: list = []
        self.pipeline: MicPipeline | None = None
        self.mute: MuteControl | None = None
        self.stream: A2dpStream | None = None
        self.mic: MicSession | None = None
        self.echo = EchoGuard()
        self.winvol: WindowsVolume | None = None
        self._quit: asyncio.Event | None = None
        self._abort: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None

    # ------------------------------------------------------------------ status / commands
    def status(self) -> dict:
        return {
            "state": self.state, "detail": self.detail, "codec": self.settings.codec,
            "codec_name": CODECS.get(self.settings.codec, self.settings.codec),
            "connect": self.settings.connect, "mic_gain_db": self.settings.mic_gain_db,
            "muted": bool(self.pipeline and self.pipeline.muted),
            "mic_live": bool(self.mic and self.mic.live), "recorders": list(self.recorders),
            "battery": dict(self.battery), "ears": self.ears, "volume_sync": self.settings.volume_sync,
        }

    def _changed(self) -> None:
        for cb in list(self.listeners):
            with contextlib.suppress(Exception):
                cb()

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        self._changed()

    def _call(self, fn: Callable[[], None]) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(fn)

    def connect(self) -> None:
        def go():
            self.settings.connect = True
            self.settings.save()
            say("control: Connect")
            self._wake.set()
            self._changed()
        self._call(go)

    def disconnect(self) -> None:
        """Release the AirPods (e.g. so the iPhone can take them) until connect()."""
        def go():
            self.settings.connect = False
            self.settings.save()
            say("control: Disconnect (release AirPods)")
            self._abort.set()
            self._wake.set()
            self._changed()
        self._call(go)

    def set_codec(self, codec: str) -> None:
        if codec not in CODECS:
            return

        def go():
            if codec == self.settings.codec:
                return
            self.settings.codec = codec
            self.settings.save()
            say(f"control: codec -> {codec} (reconnecting; the AirPods reject reconfiguration)")
            self._abort.set()
            self._changed()
        self._call(go)

    def set_mic_gain(self, db: float) -> None:
        def go():
            self.settings.mic_gain_db = float(db)
            self.settings.save()
            if self.pipeline:
                self.pipeline.gain = float(10 ** (db / 20))
            say(f"control: mic gain {db:+.0f} dB")
            self._changed()
        self._call(go)

    def toggle_mute(self) -> None:
        def go():
            if self.mute:
                self.mute.toggle("tray")
            self._changed()
        self._call(go)

    def set_volume_sync(self, on: bool) -> None:
        def go():
            self.settings.volume_sync = on
            self.settings.save()
            self._changed()
        self._call(go)

    def quit(self) -> None:
        self._call(lambda: self._quit.set())

    # ------------------------------------------------------------------ volume sync
    def _windows_volume_changed(self, scalar: float, muted: bool) -> None:  # COM thread
        self._call(lambda: asyncio.ensure_future(self._push_volume_to_airpods(scalar, muted)))

    async def _push_volume_to_airpods(self, scalar: float, muted: bool) -> None:
        if not self.settings.volume_sync or not self.stream:
            return
        target = scalar_to_abs(scalar, muted)
        if self.echo.is_echo("windows", target):
            return  # this slider move was us mirroring the AirPods
        with contextlib.suppress(Exception):
            if await self.stream.set_volume(target):
                self.echo.note("airpods", target)
                say(f"volume sync: Windows {scalar * 100:.0f}%{' muted' if muted else ''} -> AirPods {target}/127")

    def _airpods_volume_changed(self, value: int) -> None:  # asyncio loop
        if not self.settings.volume_sync or not self.winvol:
            return
        if self.echo.is_echo("airpods", value):
            return  # echo of our own SetAbsoluteVolume
        self.echo.note("windows", value)
        if value == 0:
            self.winvol.set(muted=True)
        else:
            self.winvol.set(scalar=value / 127, muted=False)
        say(f"volume sync: AirPods {value}/127 -> Windows {value * 100 / 127:.0f}%")

    def _initial_volume(self) -> int | None:
        if not (self.settings.volume_sync and self.winvol):
            return None  # leave the AirPods' own volume alone (no more forcing 40)
        scalar, muted = self.winvol.current
        target = scalar_to_abs(scalar, muted)
        self.echo.note("airpods", target)
        return target

    # ------------------------------------------------------------------ AACP status
    def _on_aacp_control(self, opcode: int, data: bytes) -> None:
        if opcode == Opcode.EAR_DETECTION and len(data) >= 2:
            ears = (EAR_STATES.get(data[0], hex(data[0])), EAR_STATES.get(data[1], hex(data[1])))
            if ears != self.ears:
                for side, old, new in zip(("primary", "secondary"), self.ears, ears):
                    if new == "disconnected" and old != "disconnected":
                        say(f"WARNING: {side} bud lost its link (ear detection {ears})")
                self.ears = ears
                self._changed()
        elif opcode == Opcode.BATTERY_INFO and len(data) >= 1:
            names = {0x01: "single", 0x02: "R", 0x04: "L", 0x08: "case"}
            battery = {}
            for i in range(data[0]):
                b = data[1 + i * 5: 6 + i * 5]
                if len(b) == 5:
                    status = BATTERY_STATUS.get(b[3], hex(b[3]))
                    battery[names.get(b[0], hex(b[0]))] = "—" if status == "disconnected" else f"{b[2]}%"
                    if status == "disconnected" and names.get(b[0]) in ("L", "R") and self.state == "connected":
                        if self.battery.get(names[b[0]], "—") != "—":
                            say(f"WARNING: {names[b[0]]} bud dropped off the link (battery report)")
            if battery != self.battery:
                self.battery = battery
                self._changed()

    # ------------------------------------------------------------------ main loop
    async def run(self) -> int:
        """Run until quit(). Returns 0, or 2 if the dongle hung (caller should restart)."""
        self.loop = asyncio.get_running_loop()
        self._quit, self._abort, self._wake = asyncio.Event(), asyncio.Event(), asyncio.Event()
        demand = None
        if self.mic_enabled:
            self.pipeline = MicPipeline(gain_db=self.settings.mic_gain_db)
            say(f"mic output: {self.pipeline.start()}")
            self.mute = MuteControl(lambda m: (setattr(self.pipeline, "muted", m), self._changed()),
                                    lambda: self.pipeline.muted)
            say(f"mute hotkey Ctrl+Alt+M: {'registered' if self.mute.start_hotkey(self.loop) else 'unavailable'}")
            if self.mic_mode != "always":
                async def apply(on: bool, recs) -> None:
                    self.recorders = recs
                    who = ", ".join(f"{n} ({p})" for p, n in recs) or "none"
                    say(f"mic demand: {'recording app(s): ' + who if on else 'no recorder'} -> mic {'ON' if on else 'OFF'}")
                    if self.mic:
                        await (self.mic.mic_on() if on else self.mic.mic_off())
                    self._changed()
                demand = MicDemandWatcher(lambda on, recs: self._call(lambda: asyncio.ensure_future(apply(on, recs))))
                demand.start()
        self.winvol = WindowsVolume(PLAYBACK, self._windows_volume_changed)
        self.winvol.start()
        try:
            return await self._device_loop(demand)
        finally:
            if demand:
                demand.stop()
            if self.winvol:
                self.winvol.stop()
            if self.pipeline:
                self.pipeline.stop()

    def _done(self) -> bool:
        return self._quit.is_set() or bool(self.deadline and time.monotonic() > self.deadline)

    async def _device_loop(self, demand) -> int:
        try:
            async with open_device(self.spec) as (device, avrcp_protocol):
                incoming = asyncio.Event()
                device.on("connection", lambda _c: incoming.set())
                sessions, retry = 0, RETRY_MIN
                connectable = True
                while not self._done():
                    if not self.settings.connect:
                        if connectable:
                            with contextlib.suppress(Exception):
                                await device.set_connectable(False)
                            connectable = False
                        self._set_state("released", "AirPods free for other devices")
                        self._wake.clear()
                        await _wait_any(self._quit.wait(), self._wake.wait())
                        continue
                    if not connectable:
                        with contextlib.suppress(Exception):
                            await device.set_connectable(True)
                        connectable = True
                    incoming.clear()
                    self._abort.clear()
                    sessions += 1
                    say(f"--- session {sessions}: connecting to {self.address}")
                    self._set_state("connecting")
                    started = time.monotonic()
                    try:
                        await self._session(device, avrcp_protocol, demand)
                        if time.monotonic() - started > 30:
                            retry = RETRY_MIN
                    except CommandTimeoutError as error:
                        raise ControllerHung(repr(error)) from error
                    except (BaseBumbleError, RuntimeError, asyncio.TimeoutError) as error:
                        say(f"--- session {sessions} ended: {error!r}")
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                        say(f"--- session {sessions} ended: link dropped during setup")
                    if self._done():
                        break
                    if self._abort.is_set():
                        continue  # codec change / disconnect: act immediately
                    incoming.clear()
                    if self.pipeline:
                        self.pipeline.worn = True
                    self._set_state("waiting", f"retry in {retry:.0f} s")
                    say(f"--- waiting for the AirPods (retry in {retry:.0f} s, or sooner if they connect to us)")
                    self._wake.clear()
                    await _wait_any(self._quit.wait(), incoming.wait(), self._wake.wait(), timeout=retry)
                    retry = min(retry * 1.5, RETRY_MAX)
        except (ControllerHung, CommandTimeoutError) as error:  # also during power-on/reset
            say(f"dongle stopped answering HCI commands ({error!r}); trying a USB reset")
            self._set_state("error", "dongle hung - resetting")
            say(reset_dongle(self.spec))
            return 2
        return 0

    async def _session(self, device, avrcp_protocol, demand) -> None:
        connection = await asyncio.wait_for(connect_airpods(device, self.address), CONNECT_TIMEOUT)
        gone = asyncio.Event()
        connection.on("disconnection", lambda r: (gone.set(), say(f"link lost: {hci.HCI_Constant.error_name(r)}")))
        codec = self.settings.codec
        source = LiveSource(codec=codec, device=self.capture, aac_encoder=self.aac_encoder, eld_frames_per_packet=1)
        self.stream = A2dpStream(connection, avrcp_protocol, source, self._initial_volume,
                                 on_remote_volume=self._airpods_volume_changed)
        mic, ticks = None, 0
        try:
            await asyncio.wait_for(self.stream.start(), CONNECT_TIMEOUT)
            monitor = LinkMonitor(device, connection)
            # Flushable media for AAC/SBC only: AAC-ELD frames depend on previous ones, and a
            # lost packet can leave the AirPods' decoder silent until the stream restarts.
            if codec != "eld":
                say(await enable_flushable_media(device.host, connection, self.stream.stream.rtp_channel, 100))
            if self.pipeline:
                mic = MicSession(connection, avrcp_protocol.delegate, self.pipeline, hotkey=False, mute=self.mute)
                mic.on_idle_key = media_key_forwarder
                mic.control_listeners.append(self._on_aacp_control)
                self.mic = mic
                await mic.start(mic=self.mic_mode == "always" or bool(demand and demand.in_use))
            self._set_state("connected")
            while not (gone.is_set() or self._abort.is_set() or self._done()):
                await _wait_any(gone.wait(), self._abort.wait(), self._quit.wait(), timeout=1.0)
                ticks += 1
                if mic:
                    await mic.watchdog()
                for note in (await source.watchdog(), self.pipeline.watchdog() if self.pipeline else None):
                    if note:
                        say(note)
                if ticks % self.link_every == 0 and not gone.is_set():
                    say(await monitor.poll(afh=ticks % (self.link_every * 6) == 0))
                if ticks % self.stats_every == 0:
                    say(f"OUT: {source.report()}")
                    if mic and mic.wanted:
                        say(f"MIC: {self.pipeline.report()}")
        finally:
            self.mic = None
            if mic:
                with contextlib.suppress(Exception):
                    await mic.stop()
            with contextlib.suppress(Exception):
                await self.stream.close()
            self.stream = None
            self.battery, self.ears = {}, ("?", "?")
            if not gone.is_set():
                await disconnect(connection)
