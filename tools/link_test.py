"""Bring up a BR/EDR link to the AirPods step by step and report where it fails.

Usage: python tools/link_test.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--sc] [--no-role-switch] [--no-auth] [--idle=SECONDS]
Steps: connect -> authenticate -> encrypt -> idle 3 s -> SDP query for A2DP sink.
"""
import asyncio
import logging
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bumble import hci  # noqa: E402
from bumble.core import ConnectionError as BumbleConnectionError  # noqa: E402
from bumble.core import PhysicalTransport  # noqa: E402
from bumble.device import Device, DeviceConfiguration  # noqa: E402
from bumble.pairing import PairingConfig  # noqa: E402
from bumble.transport import open_transport  # noqa: E402
from bumble.avdtp import Protocol as AvdtpProtocol  # noqa: E402
from bumble.avdtp import find_avdtp_service_with_connection  # noqa: E402

from airpodlink.hci_quirks import apply_event_trim_quirk  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
T0 = time.monotonic()


def say(msg):
    print(f"[{time.monotonic() - T0:6.2f}s] {msg}", flush=True)


async def main(spec, address, sc, role_switch, auth, idle):
    transport = await open_transport(spec)
    apply_event_trim_quirk(transport)
    async with transport:
        config = DeviceConfiguration()
        config.load_from_file(str(pathlib.Path(__file__).resolve().parents[1] / "config" / "device.json"))
        config.classic_enabled = True
        config.le_enabled = False
        config.classic_sc_enabled = sc
        device = Device.from_config_with_hci(config, transport.source, transport.sink)
        device.pairing_config_factory = lambda c: PairingConfig(mitm=False)
        await device.power_on()
        say(f"powered on, SC={'on' if sc else 'off'}, role switch={'allowed' if role_switch else 'off'}")

        dropped = asyncio.Event()

        if role_switch:
            try:
                connection = await device.connect(address, transport=PhysicalTransport.BR_EDR)
            except BumbleConnectionError as error:
                if error.error_code != hci.HCI_CONNECTION_ALREADY_EXISTS_ERROR:
                    raise
                # The AirPods reconnected to us on their own (they auto-connect to a
                # known host) and raced our page. Use their incoming link.
                await asyncio.sleep(0.5)
                connection = device.find_connection_by_bd_addr(hci.Address(address))
                if connection is None:
                    raise
                say("AirPods connected to us first (incoming link)")
        else:
            # Device.connect() always allows role switch; issue Create_Connection ourselves.
            fut = asyncio.get_running_loop().create_future()
            device.once("connection", fut.set_result)
            await device.send_command(hci.HCI_Create_Connection_Command(
                bd_addr=hci.Address(address), packet_type=0xCC18, page_scan_repetition_mode=2,
                clock_offset=0, allow_role_switch=0, reserved=0))
            connection = await asyncio.wait_for(fut, 15)
        say(f"connected, role={connection.role_name}")

        def on_disc(reason):
            say(f"DISCONNECTED: {hci.HCI_Constant.error_name(reason)}")
            dropped.set()

        connection.on("disconnection", on_disc)
        connection.on("role_change", lambda role: say(f"role change -> {role}"))
        connection.on("connection_mode_change", lambda: say(f"mode change -> {connection.classic_mode}"))

        async def step(name, coro, timeout=10):
            say(f"{name} ...")
            task = asyncio.ensure_future(coro)
            done, _ = await asyncio.wait([task, asyncio.ensure_future(dropped.wait())],
                                         timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if task in done and not task.cancelled() and task.exception() is None:
                say(f"{name} OK -> {task.result()}")
                return True
            if task in done and (task.cancelled() or task.exception() is not None):
                say(f"{name} FAILED: {'cancelled' if task.cancelled() else repr(task.exception())}")
            elif dropped.is_set():
                say(f"{name} interrupted by disconnection")
            else:
                say(f"{name} timed out")
            task.cancel()
            return False

        ok = await step("authenticate", connection.authenticate()) if auth else True
        ok = ok and await step("encrypt", connection.encrypt())
        if ok and idle:
            say(f"idle {idle} s with encryption on ...")
            try:
                await asyncio.wait_for(dropped.wait(), idle)
                ok = False
            except asyncio.TimeoutError:
                say("still connected after idle")
        ok = ok and await step("SDP: find AVDTP version", find_avdtp_service_with_connection(connection))
        if ok:
            version = await find_avdtp_service_with_connection(connection)
            avdtp = await AvdtpProtocol.connect(connection, version)
            for ep in await avdtp.discover_remote_endpoints():
                say(f"endpoint seid={ep.seid} type={'SINK' if ep.tsep else 'SOURCE'} in_use={ep.in_use}")
                for cap in ep.capabilities:
                    say(f"    {cap}")
            say("holding link 5 s ...")
            try:
                await asyncio.wait_for(dropped.wait(), 5)
            except asyncio.TimeoutError:
                say("link stable")
            if not dropped.is_set():
                try:
                    await connection.disconnect()
                except hci.HCI_Error:
                    pass  # raced with a remote disconnect
        say("PASS" if ok else "FAIL")


args = [a for a in sys.argv[1:] if not a.startswith("--")]
idle_arg = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--idle=")), 3.0)
asyncio.run(main(args[0], args[1], "--sc" in sys.argv, "--no-role-switch" not in sys.argv,
                 "--no-auth" not in sys.argv, idle_arg))
