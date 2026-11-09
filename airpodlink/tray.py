"""System-tray icon for the bridge (pystray): status, connect/disconnect, codec, mic gain, mute.

Runs on its own thread; every action goes through Bridge's thread-safe commands.
"""

from __future__ import annotations

import os
import pathlib
import threading

import pystray
from PIL import Image, ImageDraw

from airpodlink.bridge import CODECS, Bridge
from airpodlink.fdk_aac import DLL_PATH

COLORS = {"connected": (52, 168, 83), "connecting": (66, 133, 244), "waiting": (66, 133, 244),
          "released": (140, 140, 140), "starting": (140, 140, 140), "error": (219, 68, 55)}
GAINS = [0, 6, 12, 18]
LOG = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "airpodlink" / "bridge.log"


def _icon_image(state: str, muted: bool, mic_live: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = COLORS.get(state, (140, 140, 140))
    # two "buds": stems + heads
    for x in (16, 40):
        d.ellipse((x - 2, 8, x + 12, 24), fill=c)
        d.rounded_rectangle((x + 2, 18, x + 8, 50), radius=3, fill=c)
    if mic_live:  # small mic dot: red when muted, white when live
        d.ellipse((44, 44, 62, 62), fill=(219, 68, 55) if muted else (255, 255, 255), outline=c, width=3)
    return img


class Tray:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.icon = pystray.Icon("airpodlink", _icon_image("starting", False, False), "AirPods bridge",
                                 menu=self._menu())
        bridge.listeners.append(self.refresh)

    # ------------------------------------------------------------------ menu
    def _status_line(self, _item=None) -> str:
        s = self.bridge.status()
        state = {"connected": "Connected", "connecting": "Connecting…", "waiting": "Waiting for AirPods",
                 "released": "Released (other devices can connect)", "starting": "Starting…",
                 "error": f"Problem: {s['detail']}"}.get(s["state"], s["state"])
        return f"{state} · {s['codec_name']}" if s["state"] == "connected" else state

    def _battery_line(self, _item=None) -> str:
        b = self.bridge.status()["battery"]
        parts = [f"{k} {b[k]}" for k in ("L", "R", "case", "single") if k in b]
        return "Battery: " + (", ".join(parts) if parts else "—")

    def _mic_line(self, _item=None) -> str:
        s = self.bridge.status()
        if s["mic_live"]:
            who = ", ".join(n for _, n in s["recorders"]) or "an app"
            return f"Mic: {'MUTED' if s['muted'] else 'live'} ({who})"
        return f"Mic: off{' (muted)' if s['muted'] else ''}"

    def _menu(self) -> pystray.Menu:
        def b() -> Bridge:  # the current bridge (service mode recreates it after restarts)
            return self.bridge

        codec_items = [pystray.MenuItem(name, (lambda c: lambda _i, _it: b().set_codec(c))(code),
                                        checked=(lambda c: lambda _i: b().settings.codec == c)(code), radio=True)
                       for code, name in CODECS.items() if code != "eld" or DLL_PATH.exists()]
        gain_items = [pystray.MenuItem(f"{g:+d} dB" if g else "0 dB", (lambda g: lambda _i, _it: b().set_mic_gain(g))(g),
                                       checked=(lambda g: lambda _i: round(b().settings.mic_gain_db) == g)(g), radio=True)
                      for g in GAINS]
        return pystray.Menu(
            pystray.MenuItem(self._status_line, None, enabled=False),
            pystray.MenuItem(self._battery_line, None, enabled=False),
            pystray.MenuItem(self._mic_line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Connect AirPods", lambda _i, _it: b().connect(),
                             visible=lambda _i: not b().settings.connect),
            pystray.MenuItem("Disconnect (let iPhone/other devices take them)", lambda _i, _it: b().disconnect(),
                             visible=lambda _i: b().settings.connect),
            pystray.MenuItem("Mute mic (Ctrl+Alt+M / stem click)", lambda _i, _it: b().toggle_mute(),
                             checked=lambda _i: b().status()["muted"]),
            pystray.MenuItem("Codec", pystray.Menu(*codec_items)),
            pystray.MenuItem("Mic gain", pystray.Menu(*gain_items)),
            pystray.MenuItem("Sync volume with Windows", lambda _i, _it: b().set_volume_sync(not b().settings.volume_sync),
                             checked=lambda _i: b().settings.volume_sync),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", lambda _i, _it: os.startfile(str(LOG)) if LOG.exists() else None),
            pystray.MenuItem("Quit bridge", self._quit),
        )

    # ------------------------------------------------------------------ lifecycle
    def refresh(self) -> None:
        s = self.bridge.status()
        self.icon.icon = _icon_image(s["state"], s["muted"], s["mic_live"])
        self.icon.title = f"AirPods bridge - {self._status_line()}"[:127]
        self.icon.update_menu()

    def _quit(self, _icon, _item) -> None:
        self.bridge.quit()
        self.icon.stop()

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def stop(self) -> None:
        self.icon.stop()
