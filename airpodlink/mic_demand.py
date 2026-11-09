"""On-demand mic: detect when any app records from the AirPods microphone endpoint.

macOS only switches the AirPods mic on while something records. Keeping the AACP mic stream
running all the time puts the AirPods in a call-like mode (AUDIO_SOURCE byte 02 -> 22) and
the user heard more music dropouts with it on, so the bridge starts/stops it on demand.

Detection: Windows Core Audio session enumeration (IAudioSessionManager2) on the capture
endpoint "AirPods Pro Microphone"; a session in state Active = an app is capturing. Polled on
its own COM thread every 0.5 s. Start is immediate; stop waits `release_s` with no recorder so
apps that briefly reopen the device don't make the mic flap.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

AUDIO_SESSION_STATE_ACTIVE = 1


def _find_capture_device(names: list[str]):
    from pycaw.constants import DEVICE_STATE, EDataFlow
    from pycaw.pycaw import AudioUtilities

    devices = AudioUtilities.GetAllDevices(EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value)
    by_name = {d.FriendlyName: d for d in devices}
    for n in names:
        if n in by_name:
            return by_name[n]
    return None


def active_recorders(device) -> list[tuple[int, str]]:
    """(pid, process name) of sessions actively capturing from `device`, excluding us."""
    from pycaw.api.audiopolicy import IAudioSessionControl2

    out = []
    enum = device.AudioSessionManager.GetSessionEnumerator()
    for i in range(enum.GetCount()):
        ctl = enum.GetSession(i).QueryInterface(IAudioSessionControl2)
        if ctl.GetState() != AUDIO_SESSION_STATE_ACTIVE:
            continue
        pid = ctl.GetProcessId()
        if pid in (0, os.getpid()):
            continue
        name = "?"
        try:
            import psutil

            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 - name is cosmetic
            pass
        out.append((pid, name))
    return out


class MicDemandWatcher:
    def __init__(self, on_change: Callable[[bool, list[tuple[int, str]]], None],
                 poll_s: float = 0.5, release_s: float = 3.0) -> None:
        from airpodlink.devices import MIC

        self.names = list(MIC)  # pycaw FriendlyName == WASAPI device name
        self.on_change = on_change
        self.poll_s = poll_s
        self.release_s = release_s
        self.in_use = False
        self.recorders: list[tuple[int, str]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mic-demand", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import comtypes

        comtypes.CoInitialize()
        try:
            device = None
            last_seen = 0.0
            while not self._stop.wait(self.poll_s):
                try:
                    device = device or _find_capture_device(self.names)
                    recs = active_recorders(device) if device else []
                except Exception:  # noqa: BLE001 - endpoint changed (audio service restart): re-find
                    device, recs = None, []
                now = time.monotonic()
                if recs:
                    last_seen = now
                    self.recorders = recs
                want = bool(recs) or (self.in_use and now - last_seen < self.release_s)
                if want != self.in_use:
                    self.in_use = want
                    self.on_change(want, self.recorders if want else [])
        finally:
            comtypes.CoUninitialize()
