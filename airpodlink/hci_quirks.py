"""Workarounds for controller firmware quirks seen with Bumble's USB transport.

Barrot 33FA:0012 (UGREEN 75073): some Command Complete events arrive as one USB
interrupt transfer that is one byte longer than the event's own length field says
(e.g. HCI_Read_Local_Extended_Features: 17 bytes on the wire for a 16-byte event).
Windows' BTHUSB treats each interrupt transfer as exactly one event, so the stray
byte is harmless there. Bumble's EventPacketSplitter treats the endpoint as a byte
stream, so the stray byte becomes the first byte of the next event and every event
after it is mis-parsed (Bumble then hangs waiting for a Command Complete).

Fix: per USB transfer, when the splitter is at a packet boundary and the transfer is
longer than the event it starts, emit that event and drop the excess. Events that
span several transfers (longer than the endpoint's max packet size) still go through
the normal accumulation path.
"""

from __future__ import annotations

import logging

from bumble import hci
from bumble.transport.usb import EventPacketSplitter

logger = logging.getLogger(__name__)


class TrimmingEventSplitter(EventPacketSplitter):
    def __init__(self, emit) -> None:
        super().__init__(emit)
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> None:
        if not self.packet and len(data) >= 2:
            event_length = 2 + data[1]
            if len(data) > event_length:
                excess = bytes(data[event_length:])
                if not self.dropped_bytes:
                    logger.warning(
                        "controller sent %d trailing byte(s) after event 0x%02X "
                        "(len %d): %s - dropping (further drops logged at DEBUG)",
                        len(excess), data[0], data[1], excess.hex(),
                    )
                else:
                    logger.debug("dropping trailing bytes %s", excess.hex())
                self.dropped_bytes += len(excess)
                self.emit(bytes(data[:event_length]))
                return
        super().feed(data)


def _find_usb_source(transport):
    """Return the object holding Bumble's USB splitters, or None if not a USB transport."""
    seen = set()
    stack = [transport]
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(getattr(obj, "splitters", None), dict):
            return obj
        for attr in ("source", "transport", "wrapped_transport"):
            stack.append(getattr(obj, attr, None))
    return None


def apply_event_trim_quirk(transport) -> bool:
    """Install TrimmingEventSplitter on an opened Bumble USB transport. Returns True if applied."""
    source = _find_usb_source(transport)
    if source is None:
        logger.info("not a Bumble USB transport; event trim quirk not applied")
        return False
    old = source.splitters[hci.HCI_EVENT_PACKET]
    source.splitters[hci.HCI_EVENT_PACKET] = TrimmingEventSplitter(old.emit)
    return True


# ---------------------------------------------------------------------------
# L2CAP: accept the Flush Timeout configuration option.
#
# The AirPods' AACP channel (PSM 0x1001) configure request carries FLUSH_TIMEOUT = 30 ms
# (options 02 02 1e 00) alongside MTU 2582 and basic mode. Bumble 0.0.235 rejects any
# option it doesn't implement with FAILURE_UNKNOWN_OPTIONS; the AirPods re-send forever
# and the channel never opens. Flush timeout is a standard option and only constrains
# the *sender's* (AirPods') retransmissions, so we accept it by stripping it from the
# request before Bumble parses it: an option omitted from a successful response is
# accepted as proposed (Core spec Vol 3 Part A 4.5).

_FLUSH_TIMEOUT = 0x02


def _strip_config_option(options: bytes, option_type: int) -> tuple[bytes, list[bytes]]:
    kept, stripped = bytearray(), []
    i = 0
    while i + 2 <= len(options):
        length = options[i + 1]
        tlv = options[i:i + 2 + length]
        (stripped.append(bytes(tlv)) if options[i] & 0x7F == option_type else kept.extend(tlv))
        i += 2 + length
    return bytes(kept), stripped


def install_l2cap_flush_timeout_accept() -> None:
    from bumble import l2cap

    if getattr(l2cap.ClassicChannel, "_airpodlink_flush_patch", False):
        return
    original = l2cap.ClassicChannel.on_configure_request

    def on_configure_request(self, request):
        options, stripped = _strip_config_option(request.options, _FLUSH_TIMEOUT)
        if stripped:
            logger.debug("accepting L2CAP flush timeout option(s) %s on cid %s",
                         [s.hex() for s in stripped], getattr(self, "source_cid", "?"))
            request.options = options
        return original(self, request)

    l2cap.ClassicChannel.on_configure_request = on_configure_request
    l2cap.ClassicChannel._airpodlink_flush_patch = True
