"""Controller bring-up check: full Bumble Host.reset() plus a feature summary.

Usage (Windows venv python):  python tools/hci_probe.py usb:1-2.4.2 [--no-quirk]

Hard timeouts throughout, so it cannot hang like bumble-controller-info.
Set BUMBLE_LOGLEVEL=DEBUG for HCI traces.
"""
import asyncio
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bumble import hci  # noqa: E402
from bumble.host import Host  # noqa: E402
from bumble.transport import open_transport  # noqa: E402

from airpodlink.hci_quirks import apply_event_trim_quirk  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())

# Features that matter for A2DP + AACP over BR/EDR.
INTERESTING = [
    "ENHANCED_DATA_RATE_ACL_2_MBPS_MODE",
    "ENHANCED_DATA_RATE_ACL_3_MBPS_MODE",
    "LMP_3_SLOT_ENHANCED_DATA_RATE_ACL_PACKETS",
    "LMP_5_SLOT_ENHANCED_DATA_RATE_ACL_PACKETS",
    "ROLE_SWITCH",
    "SNIFF_MODE",
    "SECURE_SIMPLE_PAIRING_CONTROLLER_SUPPORT",
    "SECURE_CONNECTIONS_CONTROLLER_SUPPORT",
    "NON_FLUSHABLE_PACKET_BOUNDARY_FLAG",
    "EXTENDED_SCO_LINK_EV3_PACKETS",
]


async def main(spec: str, quirk: bool) -> int:
    transport = await asyncio.wait_for(open_transport(spec), 10)
    async with transport:
        if quirk:
            print(f"event trim quirk applied: {apply_event_trim_quirk(transport)}")
        host = Host(transport.source, transport.sink)
        try:
            await asyncio.wait_for(host.reset(), 15)
        except asyncio.TimeoutError:
            print("FAIL: Host.reset() timed out (try BUMBLE_LOGLEVEL=DEBUG)")
            return 1
        v = host.local_version
        addr = await host.send_command(hci.HCI_Read_BD_ADDR_Command())
        print("PASS: Host.reset() completed")
        print(f"  BD_ADDR     : {addr.return_parameters.bd_addr}")
        print(f"  HCI version : {hci.SpecificationVersion(v.hci_version).name} sub {v.hci_subversion}")
        print(f"  LMP version : {hci.SpecificationVersion(v.lmp_version).name} sub {v.lmp_subversion}")
        print(f"  Company ID  : {v.company_identifier} (0x{v.company_identifier:04X})")
        q = host.acl_packet_queue
        if q is not None:
            print(f"  ACL buffers : {q.max_in_flight} x {q.max_packet_size} B")
        for name in INTERESTING:
            flag = getattr(hci.LmpFeatureMask, name, None)
            state = "?" if flag is None else ("yes" if host.supports_lmp_features(flag) else "NO")
            print(f"  {name:45s} {state}")
        dropped = getattr(transport.source, "splitters", {}).get(hci.HCI_EVENT_PACKET)
        if dropped is not None and hasattr(dropped, "dropped_bytes"):
            print(f"  stray event bytes dropped: {dropped.dropped_bytes}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(asyncio.run(main(args[0], "--no-quirk" not in sys.argv)))
