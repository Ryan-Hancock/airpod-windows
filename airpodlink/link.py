"""Bumble device setup and the BR/EDR link to the AirPods.

Everything here encodes behaviour verified against AirPods Pro 3 on the Barrot dongle
(see README): the HCI event-trim quirk, authenticate-before-encrypt, and accepting the
AirPods' own incoming reconnection when it races our page.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import time

from bumble import avrcp, hci
from bumble.a2dp import make_audio_source_service_sdp_records
from bumble.core import ConnectionError as BumbleConnectionError
from bumble.core import PhysicalTransport
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.pairing import PairingConfig
from bumble.transport import open_transport

from airpodlink.hci_quirks import apply_event_trim_quirk, install_l2cap_flush_timeout_accept

ROOT = pathlib.Path(__file__).resolve().parents[1]
_T0 = time.monotonic()


def say(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:7.2f}s] {msg}", flush=True)


class LoggingAvrcpDelegate(avrcp.Delegate):
    """Logs AVRCP commands the AirPods send us (stem presses arrive as passthrough keys)."""

    def __init__(self) -> None:
        super().__init__()
        self.key_listeners: list = []

    async def on_key_event(self, key, pressed: bool, data: bytes) -> None:
        say(f"AVRCP: <- key {getattr(key, 'name', key)} {'pressed' if pressed else 'released'}")
        for listener in self.key_listeners:
            listener(key, pressed)

    async def set_absolute_volume(self, volume: int) -> None:
        say(f"AVRCP: <- AirPods set our volume to {volume}/127")
        await super().set_absolute_volume(volume)


@contextlib.asynccontextmanager
async def open_device(spec: str):
    """Yield (device, avrcp_protocol), powered on, with A2DP source + AVRCP SDP records."""
    install_l2cap_flush_timeout_accept()
    transport = await open_transport(spec)
    apply_event_trim_quirk(transport)
    async with transport:
        config = DeviceConfiguration()
        config.load_from_file(str(ROOT / "config" / "device.json"))
        config.classic_enabled = True
        config.le_enabled = False
        config.classic_sc_enabled = os.environ.get("AIRPODLINK_SC") == "1"
        device = Device.from_config_with_hci(config, transport.source, transport.sink)
        device.sdp_service_records = {
            0x00010001: make_audio_source_service_sdp_records(0x00010001),
            # Controller (we send volume) + Target (player) so the AirPods see a normal
            # media source with absolute-volume support.
            0x00010002: avrcp.ControllerServiceSdpRecord(0x00010002).to_service_attributes(),
            0x00010003: avrcp.TargetServiceSdpRecord(0x00010003).to_service_attributes(),
        }
        avrcp_protocol = avrcp.Protocol(LoggingAvrcpDelegate())
        avrcp_protocol.listen(device)
        device.pairing_config_factory = lambda c: PairingConfig(mitm=False)
        await device.power_on()
        yield device, avrcp_protocol


async def connect_airpods(device: Device, address: str) -> Connection:
    """Connect (or adopt the AirPods' incoming link), then authenticate and encrypt."""
    connection = None
    for attempt in range(3):
        try:
            connection = await device.connect(address, transport=PhysicalTransport.BR_EDR)
            break
        except (BumbleConnectionError, hci.HCI_Error) as error:
            # "already exists" arrives as ConnectionError (from Connection Complete) or as
            # HCI_Error (from Create Connection's Command Status), depending on timing.
            if error.error_code != hci.HCI_CONNECTION_ALREADY_EXISTS_ERROR:
                raise
        # The AirPods auto-reconnect to a known host and can win the race with our page.
        # Their incoming link usually shows up within ~1 s; otherwise page again.
        for _ in range(50):
            if connection := device.find_connection_by_bd_addr(hci.Address(address)):
                say("AirPods connected to us first; using their incoming link")
                break
            await asyncio.sleep(0.1)
        if connection is not None:
            break
        say(f"connection race: no incoming link appeared, paging again (attempt {attempt + 2}/3)")
    if connection is None:
        raise RuntimeError("could not establish a link to the AirPods")
    connection.on("disconnection", lambda reason: say(f"DISCONNECTED: {hci.HCI_Constant.error_name(reason)}"))
    say(f"connected ({connection.role_name})")
    # Order matters: encrypt-without-explicit-auth drops the link with LMP_PDU_NOT_ALLOWED.
    if not connection.authenticated:
        await connection.authenticate()
    if not connection.is_encrypted:
        await connection.encrypt()
    say("authenticated + encrypted")
    return connection


async def wait_disconnected(connection: Connection) -> None:
    event = asyncio.Event()
    connection.on("disconnection", lambda _reason: event.set())
    await event.wait()


async def disconnect(connection: Connection) -> None:
    with contextlib.suppress(hci.HCI_Error):
        await connection.disconnect()


def reset_dongle(spec: str) -> str:
    """Best-effort USB reset of the dongle named by a `usb:<bus>-<ports>` moniker.

    The Barrot controller sometimes stops answering HCI commands (CommandTimeoutError for
    every session) until it is replugged. libusb_reset_device is the software equivalent
    where the backend supports it. Returns a short description of what happened.
    """
    import usb1

    from bumble.transport.usb import load_libusb

    path = spec.split(":", 1)[-1].split("+")[0].rstrip("!")
    load_libusb()
    with usb1.USBContext() as ctx:
        for dev in ctx.getDeviceIterator(skip_on_error=True):
            if f"{dev.getBusNumber()}-{'.'.join(map(str, dev.getPortNumberList()))}" != path:
                continue
            try:
                handle = dev.open()
            except usb1.USBError as error:
                return f"USB reset: cannot open {path}: {error!r}"
            try:
                handle.resetDevice()
                return f"USB reset of {path}: OK"
            except usb1.USBErrorNotFound:
                return f"USB reset of {path}: device re-enumerated (OK)"
            except usb1.USBError as error:
                return f"USB reset of {path} failed: {error!r}"
            finally:
                try:
                    handle.close()
                except usb1.USBError:
                    pass
    return f"USB reset: {path} not found"
