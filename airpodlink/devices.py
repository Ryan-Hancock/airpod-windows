"""Windows audio endpoints the bridge uses, by WASAPI name, with fallbacks.

You can rename the endpoints to the friendly names below in Sound settings. Names are matched
on the *full* WASAPI name including the driver part in parentheses, because the real
AirPods (when paired to Windows' own Bluetooth) create endpoints like
"Headphones (AirPods Pro)" that a bare "AirPods Pro" match would hit.
"""

from __future__ import annotations

# role -> candidate names, preferred first (renamed), then the driver defaults.
PLAYBACK = [  # render endpoint apps play to; the bridge loopback-captures it -> A2DP
    "AirPods Pro (VB-Audio Voicemeeter VAIO)",
    "Voicemeeter VAIO3 Input (VB-Audio Voicemeeter VAIO)",
]
MIC_FEED = [  # render endpoint the bridge writes decoded mic audio into
    "AirPods Mic Feed (bridge only) (VB-Audio Virtual Cable)",
    "CABLE Input (VB-Audio Virtual Cable)",
]
MIC = [  # capture endpoint apps record from (the other end of MIC_FEED)
    "AirPods Pro Microphone (VB-Audio Virtual Cable)",
    "CABLE Output (VB-Audio Virtual Cable)",
]


def pick(candidates: list[str], available: list[str]) -> str:
    """First candidate present in `available` (exact match, then prefix match)."""
    for c in candidates:
        if c in available:
            return c
    for c in candidates:
        base = c.split(" (VB-Audio")[0]
        for a in available:
            if a.startswith(base) and "VB-Audio" in a:
                return a
    raise RuntimeError(f"none of {candidates} found among Windows audio devices "
                       "(is VB-Cable / the Voicemeeter driver installed and the endpoint enabled?)")


def wasapi_names(kind: str) -> list[str]:
    """WASAPI device names of `kind` 'input' or 'output' via sounddevice."""
    import sounddevice as sd

    wasapi = next(i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"])
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    return [d["name"] for d in sd.query_devices() if d["hostapi"] == wasapi and d[key] > 0]
