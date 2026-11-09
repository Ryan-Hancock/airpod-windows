"""Full two-way AirPods bridge for Windows (thin CLI around airpodlink.bridge.Bridge).

  Windows playback device "AirPods Pro" --(WASAPI loopback, live AAC-ELD/AAC/SBC)--> AirPods (A2DP)
  AirPods hi-res mic (AACP, AAC-ELD) --(decode)--> "AirPods Mic Feed" -> apps record "AirPods Pro Microphone"

Usage (from WSL or a Windows terminal):
  tools/winpy.sh tools/airpods_bridge.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--tray] [--codec=eld|aac|sbc]
        [--mic=auto|always] [--no-mic] [--gain-db=G] [--playback-device="<full WASAPI name>"]
        [--aac-encoder=fdk|ffmpeg] [--seconds=N]

Settings (codec, mic gain, connect/released, volume sync) persist in
%LOCALAPPDATA%\\airpodlink\\settings.json and are normally changed from the tray icon (--tray).
Command-line --codec/--gain-db override and save them.

--service (used by the logon task, see setup/autostart.ps1): headless under pythonw, logs to
%LOCALAPPDATA%\\airpodlink\\bridge.log, single instance, link stats every 10 s, restarts itself
after crashes and after a dongle hang (USB reset first). "Quit bridge" in the tray exits.
Console (non-service): 'm' + Enter toggles mute, 'q' + Enter quits.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from airpodlink.bridge import Bridge  # noqa: E402
from airpodlink.fdk_aac import DLL_PATH  # noqa: E402
from airpodlink.settings import Settings  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())


def console_thread(bridge: Bridge) -> None:
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "m":
            bridge.toggle_mute()
        elif cmd == "q":
            bridge.quit()
            return


def service_setup() -> None:
    """Headless mode: log file, single instance."""
    import ctypes
    import logging.handlers

    log_dir = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "airpodlink"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_dir / "bridge.log", maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

    class _Writer:  # route print()/say() and stray stderr into the rotating log
        def write(self, text):
            if text.strip():
                handler.emit(logging.LogRecord("bridge", logging.INFO, "", 0, text.rstrip(), None, None))

        def flush(self):
            pass

    sys.stdout = sys.stderr = _Writer()
    logging.getLogger().handlers[:] = [handler]
    # Single instance: a second copy would fight over the dongle and the mute hotkey.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    service_setup.mutex = kernel32.CreateMutexW(None, False, "Local\\airpodlink-bridge")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("another bridge instance is already running; exiting")
        sys.exit(0)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kv = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    service = "--service" in sys.argv
    if service:
        service_setup()
        print(f"service start: {' '.join(sys.argv)}")
    settings = Settings.load()
    if "codec" in kv:
        settings.codec = kv["codec"]
    if "gain-db" in kv:
        settings.mic_gain_db = float(kv["gain-db"])
    if settings.codec == "eld" and not DLL_PATH.exists():
        print("AAC-ELD needs the optional FDK AAC library (see vendor/fdk-aac/SOURCE.txt); using SBC")
        settings.codec = "sbc"
    settings.save()
    aac_encoder = kv.get("aac-encoder", "fdk" if DLL_PATH.exists() else "ffmpeg")

    tray = None
    while True:
        bridge = Bridge(args[0], args[1], settings, mic="--no-mic" not in sys.argv, mic_mode=kv.get("mic", "auto"),
                        capture=kv.get("playback-device"), stats_every=60 if service else 1,
                        link_every=10 if service else 2, aac_encoder=aac_encoder,
                        seconds=float(kv["seconds"]) if "seconds" in kv else None)
        if "--tray" in sys.argv or service:
            from airpodlink.tray import Tray

            if tray is None:
                tray = Tray(bridge)
                tray.start()
            else:  # reuse the icon across restarts
                tray.bridge = bridge
                bridge.listeners.append(tray.refresh)
        if sys.stdin and sys.stdin.isatty() and not service:
            threading.Thread(target=console_thread, args=(bridge,), daemon=True).start()
        try:
            code = 0
            with contextlib.suppress(KeyboardInterrupt):
                code = asyncio.run(bridge.run())
        except Exception as error:  # noqa: BLE001 - supervisor: log and restart
            if not service:
                raise
            print(f"bridge crashed: {error!r}; restarting in 10 s")
            time.sleep(10)
            continue
        if code == 2 and service:
            print("restarting after dongle reset in 3 s")
            time.sleep(3)
            continue
        if tray:
            tray.stop()
        return code


if __name__ == "__main__":
    sys.exit(main())
