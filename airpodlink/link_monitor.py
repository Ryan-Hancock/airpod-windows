"""Bluetooth link diagnostics and A2DP media flushability.

LinkMonitor (per connection):
  - ACL delivery time: when the host hands an ACL packet to the controller (DataPacketQueue
    send) until the controller reports it completed (HCI Number Of Completed Packets, i.e.
    the AirPods acked it at baseband). Rising values = retransmissions / interference.
  - RSSI, Link Quality, Failed Contact Counter (packets flushed or failed), and the AFH
    channel map (how many of the 79 BR/EDR channels the controller still uses).

enable_flushable_media():
  Bumble sends every ACL packet as "first, non-automatically-flushable" (PB=00), so under
  interference the controller retries an audio packet indefinitely and everything behind
  it queues - the AirPods' buffer then runs dry (a dropout) and latency creeps. Linux/Android
  send A2DP media as automatically flushable with a flush timeout instead. This marks ONLY
  the A2DP RTP channel's PDUs flushable (PB=10) and sets the link's automatic flush timeout;
  signalling, AVRCP and the AACP mic channel stay reliable (non-flushable).
"""

from __future__ import annotations

import collections
import struct
import time

import numpy as np

from bumble import hci
from bumble.device import Connection

OP_WRITE_AUTOMATIC_FLUSH_TIMEOUT = hci.hci_command_op_code(0x03, 0x0028)
OP_READ_FAILED_CONTACT_COUNTER = hci.hci_command_op_code(0x05, 0x0001)
OP_READ_LINK_QUALITY = hci.hci_command_op_code(0x05, 0x0003)
OP_READ_AFH_CHANNEL_MAP = hci.hci_command_op_code(0x05, 0x0006)


def _raw_params(response) -> bytes:
    rp = response.return_parameters
    if isinstance(rp, (bytes, bytearray)):
        return bytes(rp)
    if hasattr(rp, "data"):
        return bytes(rp.data)
    return bytes(rp)


async def raw_command(host, op_code: int, params: bytes) -> bytes:
    response = await host.send_command(hci.HCI_Command(params, op_code=op_code))
    return _raw_params(response)


class LinkMonitor:
    def __init__(self, device, connection: Connection) -> None:
        self.host = device.host
        self.connection = connection
        self.handle = connection.handle
        self.sent: collections.deque[float] = collections.deque()
        self.latencies: list[float] = []
        self.max_in_flight = 0
        self.fcc_last: int | None = None
        queue = connection.data_packet_queue  # Bumble's per-host DataPacketQueue for this link
        self.queue = queue
        if queue is not None:
            original_send = queue._send
            original_completed = queue.on_packets_completed

            def _send(packet):
                if getattr(packet, "connection_handle", None) == self.handle:
                    self.sent.append(time.perf_counter())
                original_send(packet)
                self.max_in_flight = max(self.max_in_flight, queue._in_flight + 1)

            def on_packets_completed(count, handle):
                if handle == self.handle:
                    now = time.perf_counter()
                    for _ in range(min(count, len(self.sent))):
                        self.latencies.append((now - self.sent.popleft()) * 1000)
                original_completed(count, handle)

            queue._send = _send
            queue.on_packets_completed = on_packets_completed

    async def poll(self, afh: bool = False) -> str:
        h = struct.pack("<H", self.handle)
        parts = []
        try:
            rssi = await self.host.send_command(hci.HCI_Read_RSSI_Command(handle=self.handle))
            parts.append(f"RSSI {rssi.return_parameters.rssi} dB")
        except Exception as error:  # noqa: BLE001 - diagnostics only
            parts.append(f"RSSI ? ({error.__class__.__name__})")
        try:
            lq = await raw_command(self.host, OP_READ_LINK_QUALITY, h)
            if lq and lq[0] == 0:
                parts.append(f"link quality {lq[3]}/255")
        except Exception:  # noqa: BLE001
            pass
        try:
            fc = await raw_command(self.host, OP_READ_FAILED_CONTACT_COUNTER, h)
            if fc and fc[0] == 0:
                value = struct.unpack_from("<H", fc, 3)[0]
                delta = "" if self.fcc_last is None else f" (+{(value - self.fcc_last) & 0xFFFF})"
                self.fcc_last = value
                parts.append(f"failed contacts {value}{delta}")
        except Exception:  # noqa: BLE001
            pass
        if afh:
            try:
                m = await raw_command(self.host, OP_READ_AFH_CHANNEL_MAP, h)
                if m and m[0] == 0:
                    used = sum(bin(b).count("1") for b in m[4:14])
                    parts.append(f"AFH {'on' if m[3] else 'off'}, {used}/79 channels in use")
            except Exception:  # noqa: BLE001
                pass
        if self.latencies:
            a = np.array(self.latencies)
            parts.append(f"ACL delivery p50/p95/max {np.percentile(a, 50):.1f}/{np.percentile(a, 95):.1f}/{a.max():.1f} ms "
                         f"(n={a.size}), max in-flight {self.max_in_flight}/{self.queue.max_in_flight}")
            self.latencies.clear()
            self.max_in_flight = 0
        return "LINK: " + ", ".join(parts)


async def enable_flushable_media(host, connection: Connection, rtp_channel, flush_timeout_ms: float) -> str:
    """Mark the A2DP RTP channel's PDUs automatically flushable; set the link flush timeout."""
    units = max(1, min(0x7FF, round(flush_timeout_ms / 0.625)))
    rp = await raw_command(host, OP_WRITE_AUTOMATIC_FLUSH_TIMEOUT, struct.pack("<HH", connection.handle, units))
    if not rp or rp[0] != 0:
        return f"Write_Automatic_Flush_Timeout failed: {rp.hex() if rp else 'no response'}"
    media = (connection.handle, rtp_channel.destination_cid)
    flushable = getattr(host, "_airpodlink_flushable", set())
    flushable.add(media)
    host._airpodlink_flushable = flushable
    if not getattr(host, "_airpodlink_flush_patch", False):
        original = host.send_acl_sdu

        def send_acl_sdu(connection_handle: int, sdu: bytes) -> None:
            cid = struct.unpack_from("<H", sdu, 2)[0] if len(sdu) >= 4 else None
            if (connection_handle, cid) not in host._airpodlink_flushable:
                return original(connection_handle, sdu)
            conn = host.connections.get(connection_handle)
            queue = conn.acl_packet_queue if conn else None
            if queue is None:
                return original(connection_handle, sdu)
            size = queue.max_packet_size
            for offset in range(0, len(sdu), size):
                pdu = sdu[offset:offset + size]
                queue.enqueue(hci.HCI_AclDataPacket(
                    connection_handle=connection_handle,
                    pb_flag=hci.HCI_ACL_PB_CONTINUATION if offset else hci.HCI_ACL_PB_FIRST_FLUSHABLE,
                    bc_flag=0, data_total_length=len(pdu), data=pdu), connection_handle)
            return None

        host.send_acl_sdu = send_acl_sdu
        host._airpodlink_flush_patch = True
    return f"A2DP media (cid 0x{media[1]:04X}) flushable, automatic flush timeout {units * 0.625:.1f} ms"
