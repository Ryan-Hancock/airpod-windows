"""Run Bumble's A2DP source app (bumble-player) with our controller quirks applied.

Same CLI as `bumble-player`, e.g. (from WSL):
  tools/winpy.sh tools/a2dp_player.py --hci-transport usb:1-2.4.2 --device-config config/device.json pair AA:BB:CC:DD:EE:FF
  tools/winpy.sh tools/a2dp_player.py --hci-transport usb:1-2.4.2 --device-config config/device.json inquire AA:BB:CC:DD:EE:FF
  tools/winpy.sh tools/a2dp_player.py --hci-transport usb:1-2.4.2 --device-config config/device.json play --connect AA:BB:CC:DD:EE:FF media/test.sbc
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import os  # noqa: E402

import bumble.logging  # noqa: E402
from bumble.apps.player import player as bumble_player  # noqa: E402
from bumble.device import Device  # noqa: E402

from airpodlink.a2dp_sources import SampleClockAacPacketSource, SampleClockSbcPacketSource  # noqa: E402
from airpodlink.hci_quirks import apply_event_trim_quirk  # noqa: E402

_original_open_transport = bumble_player.open_transport


async def _open_transport_with_quirks(spec):
    transport = await _original_open_transport(spec)
    apply_event_trim_quirk(transport)
    return transport


bumble_player.open_transport = _open_transport_with_quirks

# Bumble's packet sources write RTP timestamps in milliseconds; use sample-clock ones.
# AIRPODLINK_MS_TIMESTAMPS=1 keeps Bumble's originals (for A/B testing).
if os.environ.get("AIRPODLINK_MS_TIMESTAMPS") != "1":
    bumble_player.SbcPacketSource = SampleClockSbcPacketSource
    bumble_player.AacPacketSource = SampleClockAacPacketSource

# bumble-player hard-codes classic_sc_enabled=False (legacy P-192 pairing + E0
# encryption). AIRPODLINK_SC=1 turns BR/EDR Secure Connections back on.
if os.environ.get("AIRPODLINK_SC") == "1":
    _original_from_config_with_hci = Device.from_config_with_hci

    def _from_config_with_sc(config, hci_source, hci_sink):
        config.classic_sc_enabled = True
        return _original_from_config_with_hci(config, hci_source, hci_sink)

    bumble_player.Device.from_config_with_hci = staticmethod(_from_config_with_sc)

if __name__ == "__main__":
    bumble.logging.setup_basic_logging("WARNING")
    bumble_player.player_cli()  # pylint: disable=no-value-for-parameter
