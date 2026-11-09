> **Lab notebook.** These are the original build notes, kept for the measurements and protocol
> details. Dates, USB paths and driver names describe one PC. Bluetooth addresses are placeholders.
> These notes describe the original setup, where Apple AAC-ELD was the default. This repository defaults to SBC.
> For setup, use the main [README](../README.md).

# Engineering notes

Personal hobby/research project. A user-mode Bluetooth stack ([Bumble](https://github.com/google/bumble))
drives a dedicated USB Bluetooth dongle over WinUSB, so the Windows Bluetooth stack never
touches it. Goal: A2DP playback and the AirPods' high-resolution AACP mic stream at the same time,
instead of Windows dropping to HFP (8/16 kHz).

**Personal use only.** The AAC-ELD mic codec is patent-encumbered (Via LA / Fraunhofer), and
AACP is Apple-proprietary and reverse-engineered. Nothing here modifies AirPods firmware, and no
kernel drivers are written or installed. WinUSB is Microsoft's stock driver.

## Status

| Phase | State |
|---|---|
| 0. Environment / dongle check | **Done.** Bumble brings up the Barrot controller, with a firmware-quirk workaround |
| 1. Pairing + A2DP source | **Done.** SBC and AAC verified by ear (channels, clean tone, sweep) |
| 2. AACP control + raw mic packets | **Done.** Mic stream stable alone and alongside A2DP; playback unaffected by ear |
| 3. AAC-ELD decode → virtual mic | **Done.** Live mic in VB-Cable, quality OK by ear, stem-click/hotkey mute verified; gain deferred |
| 4. WASAPI loopback → A2DP | **Done.** Live two-way bridge with auto-reconnect; music clean and in sync by ear |
| 5. Latency harness + tuning | Measured: mic ≈ 189 ms, playback ≈ 185–200 ms (AirPods' 150 ms buffer dominates); mic jitter/drift tuned |

## Everyday use (installed 2026-10-02)

- **Nothing to start.** The bridge runs hidden at every logon (Task Scheduler task
  **"AirPods bridge"**, `pythonw`, no window) and connects whenever the AirPods are available.
- Windows devices: play to **"AirPods Pro"**, record from **"AirPods Pro Microphone"**. Both are
  the defaults for every role. Ignore **"AirPods Mic Feed (bridge only)"**: it's the bridge's
  internal pipe and must stay enabled.
- **Mic is on-demand, like macOS.** The AirPods' mic stream runs only while some app records
  from "AirPods Pro Microphone": it starts within ~0.5 s and stops 3 s after the last recorder
  closes. Otherwise the AirPods stay in pure music mode.
- **Stem click:** while an app is recording, it **mutes/unmutes** the mic. Otherwise it's Windows
  **media Play/Pause** (YouTube, Spotify, …). **Ctrl+Alt+M** also toggles mute.
- Log: `%LOCALAPPDATA%\airpodlink\bridge.log` (rotating; link stats every 10 s, stream stats every minute).
- **Tray icon** (two buds; green = connected, blue = connecting/waiting, grey = released, red = problem;
  a dot appears while the mic is live, red when muted). Menu: status, battery (L/R/case), mic state
  and recording app, **Connect / Disconnect** (Disconnect releases the AirPods: the dongle stops
  paging and turns non-connectable, so your iPhone can take them), **Mute mic**, **Codec**
  (AAC-ELD / AAC / SBC; switching reconnects, because the AirPods reject reconfiguration),
  **Mic gain** (0/+6/+12/+18 dB), **Sync volume with Windows**, Open log, Quit.
  Settings persist in `%LOCALAPPDATA%\airpodlink\settings.json`.
- **Volume sync:** the Windows "AirPods Pro" slider and mute ↔ the AirPods' own volume (AVRCP
  absolute volume), both ways, echo-suppressed. On connect the AirPods take the Windows slider's
  value instead of the old forced 40/127.

```powershell
# from the repo (<repo>\setup), in a normal PowerShell:
.\autostart.ps1 -Status            # task state, process count, last log lines
.\autostart.ps1 -Stop | -Start
.\autostart.ps1 -Update            # after code changes: re-copy the app to %LOCALAPPDATA%\airpodlink\app and restart
.\autostart.ps1 -Uninstall         # remove the task and the copied app
```

Running `airpods_bridge.cmd` or any `tools/*` script needs the dongle, so stop the service first
(`-Stop`). The service holds a single-instance lock and restarts itself 10 s after a fatal error,
e.g. if the dongle is unplugged. Reconnect retries back off from 5 s to 30 s while the AirPods
are away.

### Sound device tidy-up (machine-specific script, not included; done 2026-10-02)
- **Renamed:** Voicemeeter VAIO3 Input → **AirPods Pro**, CABLE Output → **AirPods Pro
  Microphone**, CABLE Input → **AirPods Mic Feed (bridge only)**. The code finds devices through
  `airpodlink/devices.py`, which accepts both old and new names.
- **Disabled** (hidden, not uninstalled): the other 16 VB-Audio/Voicemeeter endpoints.
- **Defaults** (console, multimedia, communications): AirPods Pro / AirPods Pro Microphone.
  Before this, the default mic was "Voicemeeter Out B3", a silent bus.
- Gotchas found on the way:
  - `Set-ItemProperty` is denied, because Administrators only hold *SetValue* on these keys. The
    script opens them with exactly SetValue.
  - `Disable-PnpDevice` on the endpoint devnodes reports success but the audio system ignores it.
    The script uses `IPolicyConfig.SetEndpointVisibility`, as the Sound control panel does.
- **Undo:** restore the names in Sound settings and re-enable the endpoints. Do not uninstall the VB-Cable or Voicemeeter drivers: the three AirPods endpoints are theirs.

## Hardware (verified 2026-10-01)

Two identical UGREEN 75073 dongles:

| Role | USB path (Bumble moniker) | Device Manager location | BD_ADDR |
|---|---|---|---|
| **Windows** (holds existing pairings) | `usb:1-5` | Port_#0005.Hub_#0001 | AA:BB:CC:00:00:01 |
| **Bumble** | `usb:1-2.4.2` | Port_#0002.Hub_#0004 (behind a hub) | AA:BB:CC:00:00:02 |

**Keep each dongle in its current port.** Neither dongle has a USB serial number, so the path is
the only thing that tells them apart, both for Bumble and for Windows' per-device driver choice.

| | Value |
|---|---|
| VID:PID | 33FA:0012 (rev 8891), enumerates as "BARROT Bluetooth 6.0 Adapter" / "UGREEN BT6.0 Adapter" |
| Chipset | Barrot. Windows driver `brbtusb_60.sys` v11.8.41.844 is a lower filter over BTHUSB |
| HCI / LMP | Core 6.0 (HCI sub 516, LMP sub 6), company ID 0x08E7 |
| ACL buffers | 9 × 679 B |
| EDR | 2 Mbps yes, 3-/5-slot EDR yes, **3 Mbps NO** |
| Other | Role switch, sniff, SSP, Secure Connections, non-flushable PB flag, eSCO EV3 all reported |
| AirPods | AirPods Pro 3 (per user; Windows reports Apple PID 0x2027) |
| Built-in Bluetooth | None on this PC |

## Setup (Windows)

The code lives in WSL (`<repo>`), but it must run under
**Windows Python**, because USB devices are not visible inside WSL2.

```bat
py -3.12 -m venv %USERPROFILE%\airpod-venv
%USERPROFILE%\airpod-venv\Scripts\python -m pip install bumble
```

`libusb` doesn't need a separate install. Bumble pulls in `libusb-package`, which bundles
`libusb-1.0.dll`. Tested with bumble 0.0.235, libusb1 3.4.0, libusb-package 1.0.26.4.

Useful tools:

```bat
set P=%USERPROFILE%\airpod-venv\Scripts\python
set R=<repo>
%P% %R%\tools\list_hci.py               & rem list HCI dongles + path monikers + whether WinUSB owns them
%P% %R%\tools\hci_probe.py usb:1-2.4.2  & rem full Host.reset() + feature summary (hard timeouts)
```

Don't use `bumble-controller-info` on these dongles. It hangs forever because of the firmware
quirk below. `tools/hci_probe.py` applies the workaround.

### Firmware quirk: extra trailing byte on some HCI events

The Barrot ROM sends some Command Complete events with one more byte than the event's length
field declares. Example: `Read_Local_Extended_Features` arrives as 17 bytes for a 16-byte event.
Windows' BTHUSB treats each USB interrupt transfer as one event, so it never notices. Bumble's USB
transport parses the interrupt endpoint as a byte stream, so the stray byte corrupts the next
event, and `Host.reset()` hangs waiting for a Command Complete.

`airpodlink/hci_quirks.py: apply_event_trim_quirk(transport)` replaces Bumble's event splitter
with one that trims each transfer to its declared event length. **Call it right after
`open_transport()` and before creating the Host.** Verified: without it, reset times out; with it,
reset completes. Three stray bytes are dropped during reset.

### Binding the Bumble dongle to WinUSB (Zadig)

> Zadig installs its driver per **VID:PID**. With two identical dongles, **both** get WinUSB.
> That happened here. Then hand the Windows dongle back as described below.

1. Download Zadig from <https://zadig.akeo.ie/>. Choose *Options → List All Devices*.
2. Select the dongle (USB ID `33FA 0012`), target **WinUSB**, then *Replace Driver*.
3. Check it: `tools\hci_probe.py usb:<path>` should print `PASS`.

### Giving one identical dongle back to Windows (per-device driver choice)

Do this for the **Windows** dongle (Port_#0005.Hub_#0001, `usb:1-5`):

1. In Device Manager, open *Universal Serial Bus devices*. Open each "UGREEN BT6.0 Adapter"
   and check *Details → Location information*. Pick the one at **Port_#0005.Hub_#0001**.
2. Choose *Driver → Update driver → Browse my computer → Let me pick from a list*, then select
   **BARROT Bluetooth 6.0 Adapter** (Barrot). If it's not listed, untick "Show compatible hardware".
3. It moves back under *Bluetooth*, and your existing pairings come back. Those
   pairings are stored against this dongle's address.

This choice is stored for that device instance (port-based). If you move the dongle to a different
port, Windows may pick WinUSB for it again.

### Fully reverting WinUSB (both dongles back to Windows)

1. In Device Manager, right-click each WinUSB-bound dongle → *Uninstall device*, tick
   **"Attempt to remove the driver for this device"**, then confirm. This removes the
   Zadig-generated INF (`oem120.inf` here).
2. Choose *Action → Scan for hardware changes*. The Barrot driver (`oem14.inf`) rebinds.

If WinUSB comes back, run `pnputil /enum-drivers` in an admin prompt and confirm `oem120.inf` is
the libwdi/Zadig one. Then run `pnputil /delete-driver oem120.inf /uninstall /force` and rescan.

## Phase 1: pairing and A2DP playback (SBC, then AAC)

Bumble's `bumble-player` app (`bumble/apps/player/player.py`) is a real A2DP source. It does
discovery, SSP pairing, AVDTP endpoint discovery and SBC/AAC/Opus streaming. It streams
**pre-encoded files** (raw SBC, ADTS AAC), so live capture needs our own encoder (phase 4).
`tools/a2dp_player.py` runs it unchanged with the Barrot event-trim quirk applied.

AirPods address (from Windows): `AA:BB:CC:DD:EE:FF`. Pairing keys are stored in
`%LOCALAPPDATA%\Google\Bumble\Pairing\<dongle address>.json`.

```bash
# from WSL, in the repo
tools/winpy.sh tools/make_test_audio.py          # writes media/test.sbc and media/test.aac
A="--hci-transport usb:1-2.4.2 --device-config config/device.json"
tools/winpy.sh tools/a2dp_player.py $A pair AA:BB:CC:DD:EE:FF                    # AirPods in pairing mode
tools/winpy.sh tools/a2dp_play.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF media/test.sbc --volume=40   # our A2DP source
tools/winpy.sh tools/a2dp_play.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF media/test.aac --volume=40
tools/winpy.sh tools/link_test.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF --idle=0      # step-by-step link check + codec dump
```

`tools/a2dp_play.py` is our own A2DP source built from Bumble's parts. Use it rather than
`bumble-player play`, which has two problems with these AirPods. First, it can't handle the
AirPods reconnecting to us on their own and racing our connection attempt. Second, Bumble's
packet sources write **RTP timestamps in milliseconds**, where A2DP requires sample-clock
timestamps (+1920 per 15-frame SBC packet, not +43). With Bumble's timestamps, the AirPods sent
AVDTP SUSPEND about 3 s into every SBC stream, in ear or not. With sample-clock timestamps
(`airpodlink/a2dp_sources.py`), both SBC and AAC streamed the full 30 s. That's one run each, so
the causal link is likely but not proven. `AIRPODLINK_MS_TIMESTAMPS=1` restores Bumble's
behaviour in `tools/a2dp_player.py` for an A/B test.

**Volume:** the AirPods report AVRCP absolute volume **0/127** for this host, so a stream with no
AVRCP volume control plays silently. `a2dp_play.py` advertises AVRCP CT/TG in SDP, opens AVRCP
after the stream starts, reports the current volume, and sets it with `--volume=N` (0..127). The
Pico port does the same thing, forcing 64 on connect. `bumble-player` has no AVRCP volume control,
so it stays silent with these AirPods.

**With `bumble-player`, always pass `--authenticate`.** If encryption is requested without an explicit authentication
first, the link drops with `LMP_PDU_NOT_ALLOWED` at the first L2CAP packet. This is reproducible.
Running `HCI_Authentication_Requested` first, then `Set_Connection_Encryption`, works every time.
It's probably a quirk of the Barrot controller's implicit-auth path, but unconfirmed without an
air trace.

### Phase 1 result (2026-10-01)

These ran via `tools/a2dp_play.py ... --volume=40` with the AirPods worn:

| Codec | Config | Streamed | Heard |
|---|---|---|---|
| SBC | 44.1 kHz stereo, 8 subbands, 16 blocks, loudness, bitpool 27 (~182 kbit/s) | full 30 s | L/R correct, steady tone clean, sweep complete |
| AAC-LC | 44.1 kHz stereo, 256 kbit/s, LATM via Bumble | full 30 s | same, no audible difference from SBC on test tones |

The AirPods' own "connected" chime overlapped the audio when streaming started about 1 s after
connecting. `a2dp_play.py` now sets the volume, then waits `--start-delay` (default 1.5 s) before
AVDTP START. The AirPods remember the volume per host: 0 on first connect, then 40 after we set it.

### AirPods Pro 3 A2DP endpoints (verified)

| SEID | Codec | Capabilities |
|---|---|---|
| 1 | SBC | 44.1/48 kHz, all channel modes, bitpool **2–27** |
| 2 | AAC-LC (MPEG-2/4) | 44.1/48 kHz, mono/stereo, VBR, ≤256 kbit/s |
| 3 | Apple vendor 0x004C:0x8001 | **AAC-ELD** per the Pico port's notes (`aac-eld-apple.md`): 48 kHz, 480-sample (10 ms) frames, raw AUs each with a 4-byte Apple header, 3 frames per RTP packet. A low-latency candidate for phase 5 |

All three support delay reporting. The AirPods report **150 ms** sink delay for SBC.

The pairing used legacy SSP (P-192, E0 encryption), because `bumble-player` disables BR/EDR
Secure Connections. `AIRPODLINK_SC=1` turns SC on, but the AirPods refused re-pairing outside
pairing mode, so SC is untested.

Test pattern (30 s): 0–10 s alternating beeps, LEFT 440 Hz and RIGHT 660 Hz; 10–20 s a steady
1 kHz tone (listen for clicks/dropouts); 20–30 s a sweep from 100 Hz to 12 kHz.

`tools/winpy.sh` runs the Windows venv Python from WSL and converts path arguments.

## Phase 2: AACP control channel and hi-res mic (raw packets)

```bash
tools/winpy.sh tools/mic_capture.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF --seconds=20                 # mic only
tools/winpy.sh tools/mic_capture.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF --play=media/test.aac --seconds=24   # mic + A2DP
tools/winpy.sh tools/mic_capture.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF --probe --seconds=5          # control traffic only
```

`airpodlink/aacp.py` ports librepods PR #655. It opens L2CAP PSM 0x1001, runs the init
sequence, logs every control packet, handles control commands and mic START/STOP, and demuxes
the 0x58 audio SDUs. Captures go to `captures/mic-*.sdu` (raw SDUs) and `captures/mic-*.au`
(demuxed AUs for phase 3). **These files are voice recordings**, so don't share them.

**Bumble gap fixed:** the AirPods' configure request for the AACP channel includes **Flush
Timeout = 30 ms**. Bumble rejects that option as unknown, and the channel never opens.
`hci_quirks.install_l2cap_flush_timeout_accept()` accepts it. It's installed by `link.open_device()`.

**Init sequence:** librepods' order works: handshake, 300 ms, feature flags `FF`, 300 ms,
notification request, then opcode 0x29 with 8 × FF. Sending the first three back to back, as the
Pico port does, also works. librepods' proximity-keys request (the AirPods' IRK/ENC keys) is
deliberately **not** sent.

**Control settings are only reported while A2DP is streaming.** Without an active stream, the
AirPods send battery, ear detection, connected devices and audio source, but no control
commands. During a stream they send the full dump: listening mode, conversation detect, adaptive
volume, EQ, head-tracking data and so on. The model string in INFORMATION is `A3064`. Serial
numbers are redacted from logs.

**Conversation awareness:** read as `0x28 = 02` (**off**) on these AirPods. `mic_capture.py`
turns it off for the duration of the mic when it's on, and restores it afterwards. It leaves it
alone when it's off or unknown. The on→off→restore path is **untested** here, because it was
already off.

### Measured mic stream (AirPods Pro 3, 2026-10-01)

| | Mic only (10 s) | Mic + AAC playback (24 s) |
|---|---|---|
| AUs/s | 133.5 (7.5 ms each = 480 samples @ 64 kHz, as the PR says) | 133.7 |
| Framing | 4 AUs per SDU, ~33 SDU/s (30 ms) | same |
| AU size | 69–78 B, avg 74 | same |
| Bitrate | ~80 kbit/s | ~79 kbit/s |
| AU timestamp step | always +180 (a 24 kHz clock) | same |
| Truncated SDUs / max SDU gap | 0 / 94 ms | 0 / 78 ms |
| A2DP during mic | — | no SUSPEND/ABORT; **no A2DP reset needed** (unlike librepods/Linux) |

The AirPods acknowledge START with `58 00 | 00 00 03 00 01 01 01` and STOP with
`58 00 | 00 00 02 00 04 01`. AUDIO_SOURCE (0x0E) changes its last byte `02 → 22` while the mic
is active.

Python jitter: the SDU callback runs on the asyncio loop and only timestamps and appends bytes,
which is fine at 33 SDU/s. Decode and output (phase 3) will run on a separate thread.

## Phase 3: AAC-ELD decode into a virtual microphone (VB-Cable)

```bash
tools/winpy.sh tools/decode_capture.py captures/mic-XXXX.au [--normalized]   # offline: capture -> 64 kHz WAV
tools/winpy.sh tools/replay_capture.py captures/mic-XXXX.sdu                  # offline: capture -> pipeline -> VB-Cable
tools/winpy.sh tools/mic_bridge.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--play=media/test.aac] [--gain-db=G] [--verify]
```

In Windows apps, pick **"CABLE Output (VB-Audio Virtual Cable)"** as the microphone.
Mute toggles: a **short stem click**, the global hotkey **Ctrl+Alt+M**, or `m` + Enter in a Windows
terminal. `q` + Enter quits.

**Decoder** (`airpodlink/eld.py`): PyAV/FFmpeg native AAC decoder with ASC `F8 E6 30 00` (AOT 39
ER AAC-ELD, mono, 480-sample frames, signalled as 48 kHz). The PCM is **64 kHz**: 133.3 AU/s × 480
samples. Durations confirm it, e.g. 1300 AUs → 9.75 s, matching the 9.7 s capture span. 0 decode
errors on every capture so far.

**Pipeline** (`airpodlink/mic_pipeline.py`):
- The asyncio loop only enqueues SDUs. The `mic-decode` thread demuxes, decodes and resamples
  64 → 48 kHz (PyAV resampler) into a ring buffer.
- The WASAPI callback thread pulls 10 ms blocks into "CABLE Input".
- Silence until mic START is acknowledged, and while muted, underrun or shutting down.
- Measured decode cost: **0.4–0.9 ms per 30 ms SDU** (one 3.4 ms spike at startup), so Python
  isn't a bottleneck here. WASAPI output latency is 22 ms.

**Verified end to end:** a 30 s live run recorded from "CABLE Output", as any app would see it,
gave continuous speech. 0 decode errors, 0 dropped SDUs, **3 underruns** (SDU gaps up to ~90 ms
against a 40 ms prefill), to tune in phase 5.

**Level:** the raw mic is quiet, averaging about −50 dBFS with peaks around −28 to −35 dBFS while
talking. PR #655 has an optional AGC for the same reason, with a −12 dBFS target and at most
+18 dB. `--gain-db` applies a fixed gain for now.

**Mute** (`airpodlink/mute.py`), verified 2026-10-02:
- The AirPods have no mute of their own in this mode. A **short stem click** arrives as an
  **AVRCP passthrough PAUSE key** (seen 5/5 times). A **long press** changes noise control
  (LISTENING_MODE). Neither sends anything mute-related over AACP, and the mic audio never goes
  silent by itself.
- So the bridge implements mute itself. While the mic is live, a PLAY/PAUSE key toggles mute.
  So does the global hotkey **Ctrl+Alt+M** (Windows `RegisterHotKey`; self-tested with synthesized
  keys) and `m` + Enter.
- End to end: in a live run, click → MUTED at 15.58 s and click → LIVE at 25.12 s. "CABLE Output"
  carried **bit-exact silence** for exactly that window (7.75–17.00 s of the recording, 250 ms
  resolution), with speech before and after.
- While muted, the AirPods keep sending mic data, and the bridge keeps decoding it so unmute is
  instant, but nothing reaches the virtual cable. Alternative, not implemented: send AACP mic STOP
  on mute, for stronger privacy at the cost of a ~0.3 s+ restart on unmute.
- Side effect: while the mic is live, a stem click no longer means play/pause.

**Quality:** confirmed good by ear on the normalized recording. Gain/AGC is deferred: the earlier
quiet level came from not talking properly, so it gets re-checked during real use.

## Phase 4: Windows playback to the AirPods (WASAPI loopback → live AAC/SBC)

The goal is AirPods that "just work like on a Mac": one playback device and one mic in Windows.
No Voicemeeter app is needed, and there's no game/chat split (dropped by request on 2026-10-02).

| Windows device | Role | Rename it to (Sound settings → device → Properties) |
|---|---|---|
| **Voicemeeter VAIO3 Input** (render) | Default **playback** device. The bridge loopback-captures it | `AirPods Pro` |
| **CABLE Output** (capture) | Default **recording/communications** device: the AirPods mic | `AirPods Pro Microphone` |

**Day-to-day:** double-click `airpods_bridge.cmd` in the repo folder
(`<repo>`), or from WSL:

```bash
tools/winpy.sh tools/airpods_bridge.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--codec=aac|sbc] [--no-mic] [--volume=40]
```

It runs as a service until closed. When the AirPods leave (case, range, the iPhone taking them
over), it retries every 5 s and also accepts the AirPods connecting back by themselves.

- Verified 2026-10-02: Voicemeeter's VAIO render endpoints consume audio in real time and **loop
  back cleanly with the Voicemeeter app not running**. A 2.0 s tone played in 1.99 s wall time and
  was captured at the exact level sent. Idle loopback keeps delivering silence at real-time rate,
  so A2DP never starves. This relies on Voicemeeter's *driver* staying installed. VB-Audio's
  Cable A+B is the clean replacement if Voicemeeter is ever uninstalled.
- `airpodlink/live_source.py`: PyAudioWPatch WASAPI loopback (22 ms capture latency), 10 ms
  blocks, then an `a2dp-encode` thread (PyAV) producing RTP packets.
  - **AAC-LC** 48 kHz stereo 256 kbit/s: 1024-sample frames, 46.9 packets/s, LATM via Bumble.
  - **SBC** 48 kHz stereo, bitpool 27 (198 kbit/s, 66-byte frames): 5 frames per packet
    (13.3 ms), 75 packets/s.
  - Worst-case encode cost is 4.3 ms per 10 ms block, with 0 capture overflows.
- Latency traps handled: Bumble's `MediaPacketPump` paces by wall clock, so live packets carry
  `timestamp_seconds = 0` and go out on arrival. Audio captured during connection setup is
  dropped. RTP timestamps stay sample-clock.
- `tools/airpods_bridge.py` runs A2DP live playback and the mic session on **one connection**, so
  the AirPods chime once.

**Live results (2026-10-02, music from YouTube):**
- 3 runs, ~130 s total, plus a 90 s reconnect run. Playback had **0 capture overflows and
  nothing queued**, and worst-case encode was 4.3 ms per 10 ms. The mic had 0 decode errors and
  1–3 underruns per run.
- By ear, music is clean and in sync, with a rare, barely noticeable "buffering" effect (phase 5).
- **Reconnect:** lid closed → link dropped at 30.75 s, bridge paging again at 30.77 s, AirPods taken
  out → connected 36.5 s, music back 39.6 s, mic live 42.0 s. That's ~3 s from connect to audio,
  1.5 s of which is the deliberate chime delay.
- **Ear gate:** the mic is silenced whenever no bud is in an ear, from the AirPods' ear-detection
  packets, as macOS does. Motivation: handling noise while the AirPods went into the case hit
  +20 dBFS on the mic. *Implemented, not yet confirmed live.*
- The game/chat split could later return with Voicemeeter Banana (Input + AUX → bus B1, then
  `--playback-device` / `loopback=False` on "Voicemeeter Out B1").

## Phase 5: latency measurement and tuning

```bash
tools/winpy.sh tools/latency_test.py mic --count=10          # clap test (bridge running); raw kept in captures/latency-*.npz
tools/winpy.sh tools/latency_test.py playback --count=8      # needs a reference mic that can hear an AirPod (see below)
```

### Harness (`tools/latency_test.py`) and what it can and can't measure
- All streams are captured in one process (PyAudioWPatch) and put on one timeline by stamping
  each 5 ms buffer with `perf_counter()` on arrival. Each stream's offset is the **lower
  envelope** min(arrival − sample/rate), since a sample can't arrive before it was captured.
- Self-test, two **loopback** captures of the same endpoint: **±0.2 ms**, 5/5 runs. The timeline
  method is precise.
- Caveat: **loopback vs capture streams are offset by a random per-session amount (±~50 ms)**.
  VB-Cable self-test: −51…+28 ms between runs, stdev 0 within a run. PortAudio's own ADC
  timestamps were worse: capture appeared *before* its loopback source. So absolute *playback*
  latency (loopback reference vs microphone) isn't reliable with this method. Mic latency
  (capture vs capture) is.
- The NexiGo webcam mic gates silence to exact zeros (noise suppression), so it couldn't hear the
  beeps from an AirPod held against it (8–20 dB "SNR", random positions). An AirPod taken out of the
  ear may also stop playing. **No acoustic playback number is claimed.**

### Mic: air at the AirPods → what Windows apps record on "CABLE Output"
**≈ 189 ms**: 8 consistent clap measurements over 2 rounds, 185–203 ms, round medians 191.1 / 188.8.
Onset-based, capture vs capture. Includes the AirPods' encoder and the 4-AU (30 ms) packet framing,
radio, our 80 ms jitter target, WASAPI output (22 ms) and VB-Cable's own variable buffer.

Mic tuning (measured, then simulated over 10 minutes with measured-like jitter):
- SDU arrival lateness vs the 30 ms cadence: **p99 52–67 ms, max 66–83 ms**. The old 40 ms prefill
  underran ~1× per 1–3 min live.
- Mic clock vs this PC: **−109 ppm** (from SDU count over 3 min). Without correction that drains a
  45 ms buffer in ~7 min, causing a periodic re-prefill glitch.
- New: **80 ms jitter target** plus **drift control**, which resamples the output ±≤1000 ppm (<2 cents,
  inaudible) to hold the buffer *trough* at target − 30 ms. Regulating the *mean* instead doubled
  underruns, because the level saw-tooths by one 30 ms SDU.

  | config (10 min sim, −109 ppm) | underruns |
  |---|---|
  | 40 ms, no control (old) | 5 |
  | 60 ms + control | 11 |
  | 70 ms + control | 3 |
  | **80 ms + control (default)** | **0** (also 0 at +300 ppm; 5 at an extreme −500 ppm) |
  | 80 ms, no control | 2 |

- **Live confirmation (7-minute soak, 2026-10-02): 0 underruns** (the old setup had ~1 per 1–3 min).
  Trough 46–53 ms vs a 50 ms setpoint, drift correction between −84 and +67 ppm, 0 dropped SDUs,
  0 decode errors. Playback in the same run: 19,647 RTP packets, 0 overflows, handoff max 2.0 ms,
  one unbroken session.
- Trade-off: target 60 ms saves ~20 ms of mic latency for more frequent brief dropouts.
  `MicPipeline(target_ms=...)`.

### Playback: Windows audio → AirPods
| stage | ms | |
|---|---|---|
| WASAPI loopback capture | 22 | reported |
| codec framing | AAC 0–21 (avg ~11), SBC 5×2.67 = 13 | arithmetic |
| bridge: capture arrival → L2CAP write | **0.4 median, p95 ≤2.5, max 7.2** | **measured** over 146 s |
| host ACL backlog | ≤2 packets pending (controller holds 9) | measured |
| radio + controller | a few ms | not measured |
| **AirPods playback buffer** | **150** | AirPods' AVDTP delay report (same for SBC and AAC) |
| **total** | **≈ 185–200** | estimate |

### Python jitter verdict
Not a problem at these loads, so **no Rust/C port is needed yet**. Worst encode 4.3 ms per 10 ms
block, decode <1 ms per 30 ms SDU, bridge handoff p95 ≤2.5 ms. The mic jitter is radio-side
(SDU arrival), not scheduling. Thread priority / MMCSS was therefore **not** changed;
PortAudio's WASAPI callbacks already run under MMCSS.

### Apple AAC-ELD (vendor codec 0x004C:0x8001): investigated 2026-10-02

**Does it lower the AirPods' buffer? Probably not.** `tools/codec_probe.py` configures and opens
each sink endpoint (no audio) and records the AirPods' AVDTP delay report:

| codec | config | AirPods delay report |
|---|---|---|
| SBC | 48 kHz stereo, bitpool 2–27 | 150 ms |
| AAC-LC | 48 kHz stereo, 256 kbit/s VBR | 150 ms |
| **Apple AAC-ELD** | `4C000000 0180 0080 0084 00 83E800` (macOS's, via the Pico port) | **150 ms** (configuration accepted) |

Caveat: that's the *declared* buffer at stream open. The actual ELD buffering could differ, or the
report could change once audio flows. Unverifiable here without a reliable acoustic playback
measurement (see harness caveats).

Notes: the AirPods reject a second SET_CONFIGURATION on the same AVDTP signalling session
after a CLOSE (ProtocolError), so probe each codec on a fresh connection.

**How hard is it? The encoder is easy:**
- **No compiler needed.** MSYS2 ships a prebuilt `libfdk-aac-2.dll` (fdk-aac 2.0.3, package
  `mingw-w64-ucrt-x86_64-fdk-aac`). Its only imports are Windows' KERNEL32 + UCRT, and it loads
  into the existing venv via `ctypes`. There are no fdk-aac bindings on PyPI.
- Proof of concept (scratch, not committed): AOT 39, 48 kHz stereo, granule 480, SBR off, CBR
  265 kbit/s, raw AUs. Result: 100 AUs/s × 331 B, **5 ms encoder delay**, **5.5 ms CPU per 1 s of
  audio**. Round trip through fdk's decoder: 1.00 s, 0 errors, level preserved. FFmpeg's decoder
  hits an internal bug on *stereo* ELD, which doesn't matter because the AirPods decode it.
- What's left for a live `--codec=eld`, ~2–4 h:
  - RTP packing with 3 AUs per packet, each prefixed by Apple's 4-byte header
    (`0xB0|seq>>8, seq, 0x10|len>>8, len`, 12-bit seq), timestamp +1440 per packet, payload ≤ 986 B.
  - The vendor caps.
  - The Pico port's recovery rule: on send stalls, reset the encoder, then SUSPEND→START, otherwise
    the AirPods go silent.
- Licence: FDK AAC licence + AAC-ELD patents. Personal use only.

## Sound quality fixes (2026-10-02)

A user report ("a tad bad", dropouts, worse with the mic on) led to measuring before changing
anything:

- **Encoder** (superseded 2026-10-08: default is now Apple AAC-ELD, see "Playback latency
  research"): a blind-ish A/B with the same music and mic on. **FFmpeg's built-in AAC: "terrible";
  fdk-aac: "a lot better"** (fdk + flushable: "cleaner bass and highs"). **fdk-aac is now the
  default** (`airpodlink/fdk_aac.py`, a ctypes wrapper around `vendor/fdk-aac/libfdk-aac-2.dll`,
  MSYS2's prebuilt fdk-aac 2.0.3). Offline: CBR ~254 kbit/s, 7.5 ms CPU per 1 s, 60 dB SNR round
  trip. Cost: fdk's AAC-LC encoder delay is 2048 samples (~43 ms) vs FFmpeg's ~21 ms, so about +21 ms
  playback latency. `--aac-encoder=ffmpeg` switches back.
- **Always-on mic:** starting the AACP mic switches the AirPods into a call-like mode (AUDIO_SOURCE
  `…02` → `…22`), and the user heard more dropouts with it on. Now **on-demand**
  (`airpodlink/mic_demand.py`): Core Audio session enumeration (pycaw `IAudioSessionManager2`) on the
  "AirPods Pro Microphone" capture endpoint, polled every 0.5 s, own process excluded. Verified: an
  external recorder from 15–25 s gave mic ON at 15.0 s (AirPods acked +0.27 s, recorder got real mic
  audio from its first second) and OFF at 27.5 s, with the AirPods back to `…02`. `--mic=always`
  restores the old behaviour.
- **Flushable A2DP media (default 100 ms):** Bumble sends every ACL packet as non-flushable
  (PB=00), so a packet under interference blocks everything behind it. Media PDUs (only the RTP
  channel) are now automatically flushable (PB=10), with HCI_Write_Automatic_Flush_Timeout = 100 ms.
  Honest note: in the A/B runs **0 packets were actually flushed**, so it's a safety net and not
  the cause of the perceived improvement. `--flushable-ms=0` turns it off.
- **Link monitor** (`airpodlink/link_monitor.py`, logged every 2 s, or every 60 s in service mode):
  RSSI, link quality, failed-contact counter, AFH channel map, and ACL delivery time (host → controller
  → AirPods baseband ack). During the A/B runs: RSSI −8…+8 dB (around the golden range), LQ ≥247/255,
  **0 failed contacts**, ACL delivery p95 ≤23 ms, ≤5 of 9 controller buffers used, AFH 62–70/79
  channels. **The dongle→AirPods radio link was healthy**, so remaining glitches are more likely
  inter-bud relay, Windows-side loopback glitches, or the source.

## Robustness: device format changes (2026-10-02)

Changing an endpoint's format in Sound settings (e.g. "AirPods Pro" to 96 kHz / 24-bit) makes
Windows silently tear down every open stream on it: callbacks just stop, with no error. Before
this fix, playback to the AirPods stopped until the bridge restarted, and a restart would have
refused any rate except 48 kHz.
- The loopback capture now opens at the **endpoint's own mix rate** and resamples to 48 kHz for
  A2DP (`_To48k`, PyAV). Verified at 96 kHz: exactly 47.0 AAC packets/s.
- **Watchdogs (~1×/s):** the capture reopens after 1.5 s without data, as does the mic output (no
  callbacks for 1.5 s). The mic output also uses WASAPI auto-convert, so "AirPods Mic Feed" can be
  any format.
- **Reopen alone wasn't enough** (live test: 96 → 192 → 96 → 192 → 48 kHz). Each switch was
  caught and reopened, and real music (−5 dBFS) kept going out, with ~5,000 ACL fragments/min
  delivered and 0 failures. The AirPods sent no SUSPEND, **but stayed silent**. Cause (inferred):
  during each ~1.5 s gap we sent nothing, yet RTP timestamps resumed where they left off, so to the
  AirPods every later packet was "late" and discarded. AACP opcode 0x52 changed `…0100`→`…0101`
  3 s after the last switch; it's undocumented, possibly a decoder/stream state.
  **Fix:** the encoder never leaves a gap. If no capture data arrives for 50 ms it feeds silence at
  real-time pace (`LiveSource._next_block`), so the AirPods' buffer never empties and RTP time stays
  locked to wall-clock time. Verified with a simulated stall: 46.2 packets/s through it, largest
  inter-packet gap 65 ms (was ~1.5 s). Live format switch: pending user confirmation.

## Playback latency research (2026-10-08)

**Measurement that works: round trip through the AirPods' own mic** (`tools/roundtrip_test.py`).
3 kHz bursts go into the playback endpoint, and the AirPods' hi-res mic hears its own speaker
(40–50 dB SNR, in ear). Both ends are timestamped in one process with `perf_counter()`
(loopback-capture arrival → mic SDU arrival). Within a session the stdev is **< 1 ms**, 8/8 bursts.
The mic return path is the same for every playback config, so **differences are exact**.

| config | round trip, per session | mean |
|---|---|---|
| fdk AAC-LC (default) | 255.4, 271.6 | ~264 ms |
| FFmpeg AAC-LC | 261.0 | 261 ms |
| SBC 5 frames/packet | 208.2, 201.9 | ~205 ms |
| SBC 2 frames/packet | 204.6 | 205 ms |

- **SBC is ~55–60 ms faster than AAC-LC end to end.** Our side accounts for ~50 ms of that:
  - fdk AAC-LC: 42.7 ms encoder delay (2048 samples) + 21.3 ms framing.
  - SBC: ~1.5 ms encoder delay + 5–13 ms framing.
  - fdk refuses the 960-sample LC frame; afterburner off doesn't change the delay.
- **The AirPods' buffer is set per connection:** ±6–16 ms between sessions, <1 ms within one.
  FFmpeg AAC came out *slower* than fdk in its session despite 21 ms less encoder delay. The
  declared 150 ms delay report never changes.
- Estimated absolute playback: round trip minus the mic return path (~60–75 ms, derived from the
  189 ms clap test minus our ~110–130 ms of mic buffering and Windows). **~190–200 ms with AAC,
  ~130–145 ms with SBC**, ±15 ms.

**Research findings (external):**
- Apple **Game Mode** cuts AirPods latency **151 → 60 ms** (Apple Developer Forums 764070). macOS
  logs show a "dynamic latency" request ("Down by 81 msec"), a jitter-buffer flush with a 60 ms
  timeout, and 1.1× playback speed to mask the transition. So the AirPods' buffer is
  host-controllable; the command isn't publicly documented (not in LibrePods' opcode tables).
  Capturing it from an iPhone/Mac in Game Mode (Bluetooth logging profile → sysdiagnose .pklg →
  Wireshark) is the biggest remaining lever.
- Measured AirPods latency on iOS starts ~215–220 ms and drifts down to ~155–160 ms over 20–30 min
  (Apple Developer Forums 679274), consistent with an adaptive jitter buffer.
- **airlow** (github.com/jkryspin/airlow): same user-mode WinUSB approach for AirPods Pro 2. It
  prefers SBC (measured ~70 ms AAC encoder delay), uses flush 40 ms, no sniff, sends each packet
  immediately, sets AVRCP volume before START, and notes "dead air makes the AirPods send AVRCP
  PAUSE". We already do all of this except SBC.
- **Apple AAC-ELD** (vendor codec 0x8001): 5 ms encoder delay + 10 ms frames, so it has SBC-like
  latency on our side with better quality than SBC at bitpool 27. AirPods accept the config
  (declared delay 150 ms, like the others).

### Apple AAC-ELD live (2026-10-08): `--codec=eld`
`LiveSource._encode_loop_eld`: fdk AOT 39, 48 kHz stereo, 480-sample frames, SBR off, CBR
256 kbit/s, afterburner off. Each frame gets Apple's 4-byte header
`B0|seq>>8, seq, 10|len>>8, len` (12-bit sequence from 1). Payload ≤ 986 B, RTP ts +480 per
frame. SET_CONFIGURATION `4C000000 0180 0080 0084 00 83E800`, byte-identical to macOS per the
Pico port. Offline: 100 packets/s at 1 frame/packet, 250/250 frames decode.

| ELD config | round trip (sessions) | beeps matched |
|---|---|---|
| 1 frame/packet | 207.1, 208.8 | 6/8, 8/8 |
| 2 frames/packet | 297.6 (spread 225–740) | 5/8 (bad session, not diagnosed) |
| 3 frames/packet (macOS) | 235.1 | 8/8 |

**ELD at 1 frame/packet ≈ SBC latency (~205–209 ms round trip), ~55–60 ms faster than fdk
AAC-LC**, while keeping AAC-family quality (sound check pending). The AirPods still declare
150 ms and the round trip matches SBC, so **ELD does not shrink the AirPods' own buffer**: the
gain is entirely our encoder (5 ms + 10 ms framing vs 43 + 21 ms).

**Now the default (2026-10-08):** the user A/B'd 30 s of ELD vs fdk AAC-LC with music: "about the
same". The service runs `--codec=eld --eld-frames=1` with **flushable media OFF for ELD**. ELD
frames depend on previous ones, and the Pico port reports a lost packet can leave the AirPods'
decoder silent until SUSPEND→START. Late-but-complete packets avoid that; no flushes were seen
in any test anyway. `--codec=aac` (fdk AAC-LC, flushable 100 ms) or `--codec=sbc` switch back.
Expected playback delay ≈ 135–150 ms (vs ≈ 190–200 ms with AAC-LC).

**Dongle stuck state:** after the first ELD attempt dropped mid-setup, the AirPods kept connecting
to the dongle every ~1.3 s and hanging up after 150 ms, then gave up. **Unplugging and replugging
the dongle cleared it** (user observation, also seen before), so it points at the Barrot
controller, not the AirPods. Candidate auto-fix: libusb device reset when sessions keep failing.

## General improvements (2026-10-08)

Code is now `airpodlink/bridge.py` (controller: session loop + thread-safe commands + status),
`airpodlink/tray.py` (pystray), `airpodlink/volume_sync.py`, `airpodlink/settings.py`.
`tools/airpods_bridge.py` is a thin CLI (`--tray`; service mode always shows the tray).

- **Volume (user report: "not synced to Windows levels").** Verified that WASAPI loopback captures
  *before* the endpoint volume/mute: the same level at 100%/25%/5%/muted. So the slider did nothing.
  Now mirrored onto AVRCP absolute volume (`round(scalar × 127)`, mute → 0) via
  IAudioEndpointVolumeCallback. AirPods volume notifications (stem swipe) go back onto the slider.
  Verified Windows → AirPods: 62% → 79/127 at connect, 30% → 38, 60% → 76, restore → 79.
  AirPods → Windows: pending a stem-swipe test.
- **Dongle hangs:** the log held **1,092 `CommandTimeoutError` sessions**, hours where the Barrot
  controller ignored every HCI command until replugged. Now a `CommandTimeoutError` (in a session
  or at power-on) ends the run, attempts a libusb **USB reset** of the dongle, and the service
  restarts 3 s later. Whether WinUSB's reset is enough or a physical replug is still needed is
  **unverified** until the next hang. A connect attempt once hung for 38 h, so connect and stream
  setup now time out after 25 s.
- **Right bud dropping (user report):** no clean capture yet. The only logged per-bud loss was at
  connect time on 10-07 13:01 (one bud on the link, then "low resources" disconnect). Now logged:
  `WARNING: … bud lost its link` from ear detection (state 3) and battery reports, plus link stats
  every 10 s, so the next mid-stream drop can be correlated with radio data.
- **Service control race:** Task Scheduler's restart-on-failure counted `-Stop`'s process kill as
  a failure and relaunched the *old* copy a minute later. A restart right after a kill also hit
  `USBErrorAccess` (dongle still held). `-Stop` now disables the task and waits for exit;
  `-Start`/`-Update` re-enable it.
- **Reporting the delay to Windows (asked): not possible from user mode.** Apps learn output
  latency from the endpoint's driver (IAudioClient/IAudioClock), here the VB-Audio/Voicemeeter
  virtual driver, which reports only its own small buffer. There's no API to add latency to an
  endpoint without our own audio driver (kernel, signing; see the kernel-driver discussion).
  Workarounds: per-app A/V offset where apps have one (VLC/mpv audio delay ≈ +150 ms with ELD);
  the latency reduction itself (ELD) is the general fix.

## Known issues

- **Stem click once killed the link with your iPhone nearby.** 1 of 2 clicks during live playback:
  music went very quiet, the mic stopped, and the AirPods disconnected ~3 s later. The other click
  (and 5 during file playback) just toggled mute. The leading hypothesis is Apple **automatic
  switching**: the click's "pause" let the nearby iPhone take the AirPods over. That's
  unconfirmed. The bridge now reconnects instead of exiting. A possible future fix is the AACP
  smart-routing/"owns connection" messages librepods uses to tell the AirPods this host is playing.


1. **No 3 Mbps EDR.** The controller supports only 2 Mbps EDR. That's ~1.4 Mbit/s usable
   with 2-DH5, which is plenty for AAC/SBC A2DP plus the AACP mic stream, but it leaves less
   headroom under RF interference.
2. **Barrot chipset is not a Bumble-supported chip.** There's no vendor init driver. The Windows
   filter reads a `BRExtPatchPath` registry value, so a firmware patch may exist that Bumble
   cannot load. Basic bring-up works from ROM. Behaviour under A2DP load, sniff and role switch is
   still untested.
3. **Identical dongles have no serial numbers.** Identity depends on the USB port, for both Bumble
   (`usb:1-2.4.2`) and Windows' driver choice. Moving dongles between ports can swap roles.
4. Running Windows Python from WSL via `cmd.exe` leaves the Windows process running if the WSL side
   is killed, and that process still holds the dongle. Kill stray `python.exe` processes, or run
   from a Windows terminal.
5. AirPods Pro 3 is reported working with PR #655's AACP mic sequence: one tester on Linux, and
   han-um's Pico 2 W port on Windows 11. That's a reported result, not verified here yet.
