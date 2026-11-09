"""Mic mute control: AirPods stem click (via AVRCP) and a global Windows hotkey.

Verified on AirPods Pro 3 (2026-10-02): while the AACP mic is live, a short stem click
arrives as an AVRCP passthrough PAUSE key (pressed + released); nothing is sent over AACP
and the mic audio does not go silent on its own. So the host must implement mute. A long
press changes noise control (LISTENING_MODE) and is not used for mute.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
from collections.abc import Callable

from bumble import avc

from airpodlink.link import say

PLAY_PAUSE_KEYS = {
    avc.PassThroughFrame.OperationId.PLAY,
    avc.PassThroughFrame.OperationId.PAUSE,
}


class MuteControl:
    def __init__(self, set_muted: Callable[[bool], None], get_muted: Callable[[], bool]) -> None:
        self._set = set_muted
        self._get = get_muted

    def toggle(self, source: str) -> None:
        muted = not self._get()
        self._set(muted)
        say(f"mute: {'MUTED' if muted else 'LIVE'} ({source})")

    def on_avrcp_key(self, key, pressed: bool) -> None:
        if pressed and key in PLAY_PAUSE_KEYS:
            self.toggle("stem click")

    def start_hotkey(self, loop, modifiers: str = "ctrl+alt", key: str = "M") -> bool:
        """Register a global hotkey (Windows RegisterHotKey) on its own message-loop thread."""
        if not hasattr(ctypes, "windll"):
            return False
        mods = 0x4000  # MOD_NOREPEAT
        for m in modifiers.lower().split("+"):
            mods |= {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}[m]
        ok = threading.Event()
        result = [False]

        def run() -> None:
            user32 = ctypes.windll.user32
            result[0] = bool(user32.RegisterHotKey(None, 1, mods, ord(key.upper())))
            ok.set()
            if not result[0]:
                return
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    loop.call_soon_threadsafe(self.toggle, f"{modifiers}+{key} hotkey")

        threading.Thread(target=run, name="mute-hotkey", daemon=True).start()
        ok.wait(2)
        return result[0]


VK_MEDIA_PLAY_PAUSE = 0xB3


def send_media_play_pause() -> None:
    """Press Windows' media Play/Pause key (what a stem click means when the mic is off)."""
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 2, 0)  # KEYEVENTF_KEYUP


def media_key_forwarder(key, pressed: bool) -> None:
    if pressed and key in PLAY_PAUSE_KEYS:
        send_media_play_pause()
        say("stem click -> Windows media Play/Pause")
