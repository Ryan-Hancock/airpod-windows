"""Keep Windows' "AirPods Pro" volume slider/mute and the AirPods' own volume in sync.

Verified 2026-10-08: WASAPI loopback captures the endpoint's audio *before* its volume and
mute (100%, 25%, 5% and muted all captured at the same level), so the Windows slider did
nothing to the AirPods. The fix is to mirror it onto the AirPods' AVRCP absolute volume
(0..127), like Windows' own Bluetooth stack does, and mirror the AirPods' changes (stem
swipe) back onto the slider. Mute maps to absolute volume 0.

Windows side: pycaw IAudioEndpointVolumeCallback on the endpoint (COM thread) -> asyncio loop.
Echo suppression: a change that just came from the other side is not sent back.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

ECHO_WINDOW = 1.5  # s: ignore the other side's echo of a change we just made


def scalar_to_abs(scalar: float, muted: bool) -> int:
    return 0 if muted else max(0, min(127, round(scalar * 127)))


class WindowsVolume:
    """Watches and sets the volume of a Windows render endpoint by friendly name."""

    def __init__(self, names: list[str], on_change: Callable[[float, bool], None]) -> None:
        self.names = names
        self.on_change = on_change
        self._ready = threading.Event()
        self._cmd: list = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.current: tuple[float, bool] = (1.0, False)
        self._thread = threading.Thread(target=self._run, name="volume-sync", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def set(self, scalar: float | None = None, muted: bool | None = None) -> None:
        """Thread-safe: queue a change of the Windows slider/mute (applied on the COM thread)."""
        with self._lock:
            self._cmd.append((scalar, muted))
        self._wake.set()

    def _run(self) -> None:
        import comtypes
        from pycaw.callbacks import AudioEndpointVolumeCallback
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        try:
            devices = AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
            dev = next((d for n in self.names for d in devices if d.FriendlyName == n), None)
            if dev is None:
                self._ready.set()
                return
            vol = dev.EndpointVolume
            outer = self

            class Callback(AudioEndpointVolumeCallback):
                def on_notify(self, new_volume, new_mute, event_context, channels, channel_volumes):
                    outer.current = (float(new_volume), bool(new_mute))
                    outer.on_change(float(new_volume), bool(new_mute))

            cb = Callback()
            vol.RegisterControlChangeNotify(cb)
            self.current = (float(vol.GetMasterVolumeLevelScalar()), bool(vol.GetMute()))
            self._ready.set()
            while not self._stop.is_set():
                self._wake.wait(1.0)
                self._wake.clear()
                with self._lock:
                    cmds, self._cmd = self._cmd, []
                for scalar, muted in cmds:
                    if scalar is not None:
                        vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, scalar)), None)
                    if muted is not None:
                        vol.SetMute(int(muted), None)
            vol.UnregisterControlChangeNotify(cb)
        finally:
            comtypes.CoUninitialize()


class EchoGuard:
    """Remembers values we just pushed to one side so their echo isn't bounced back."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[int, float]] = {}

    def note(self, side: str, value: int) -> None:
        self._last[side] = (value, time.monotonic())

    def is_echo(self, side: str, value: int, tolerance: int = 1) -> bool:
        v = self._last.get(side)
        return v is not None and abs(v[0] - value) <= tolerance and time.monotonic() - v[1] < ECHO_WINDOW
