# airpod

Music and the AirPods' high-resolution mic, at the same time, on Windows.

Normally, when an app opens the mic, Windows switches AirPods to the HFP call profile at 8/16 kHz.
This project gives the AirPods their own Bluetooth stack instead. [Bumble](https://github.com/google/bumble)
runs in user mode and drives a dedicated USB dongle over WinUSB, so Windows never touches that dongle.

- **Mic:** Apple's AACP protocol, AAC-ELD, 64 kHz, about 80 kbit/s.
- **Playback:** A2DP with SBC by default. [Apple AAC-ELD is optional](#optional-aac-eld-with-fdk-aac).
- **In Windows:** one normal playback device and one normal microphone.
- **No kernel drivers and no AirPods firmware changes.** WinUSB is Microsoft's stock driver.

> [!WARNING]
> This is an experimental hobby project. It has been tested on **one PC**, with **AirPods Pro 3**
> and a **UGREEN 75073 dongle** (Barrot chipset). Other dongles and AirPods may not work.
> AACP is reverse-engineered. Not affiliated with or endorsed by Apple.

## What you need

- Windows 11 and Python 3.12.
- AirPods. Only AirPods Pro 3 has been tested.
- A USB Bluetooth dongle **dedicated to this project**. Windows can't use it while the bridge does.
- [VB-Cable](https://vb-audio.com/Cable/) and [Voicemeeter Potato](https://vb-audio.com/Voicemeeter/potato.htm).
  The bridge uses their virtual devices. The Voicemeeter app doesn't need to be running.
- [Zadig](https://zadig.akeo.ie/), to bind the dongle to WinUSB.

## Setup

**1. Install the Python packages.** From the repo folder:

```bat
py -3.12 -m venv %USERPROFILE%\airpod-venv
%USERPROFILE%\airpod-venv\Scripts\python -m pip install -r requirements.txt
```

**2. Bind the dongle to WinUSB.** In Zadig, choose *Options → List All Devices*, select the dongle,
choose **WinUSB**, then *Replace Driver*.

> [!WARNING]
> Zadig installs per USB ID. If you own two identical dongles, **both** get WinUSB. To hand one back
> to Windows, use Device Manager: *Update driver → Browse my computer → Let me pick from a list*.
> Dongles without serial numbers are told apart by USB port, so keep them in the same ports.

**3. Find your two values.** List the dongles to get the `usb:<path>` value:

```bat
%USERPROFILE%\airpod-venv\Scripts\python tools\list_hci.py
```

Find the AirPods' Bluetooth address in Device Manager, if they've been paired with Windows:
open the device, then *Details → Bluetooth device address*. Write it as `AA:BB:CC:DD:EE:FF`.

**4. Pair.** Put the AirPods in pairing mode, then run:

```bat
%USERPROFILE%\airpod-venv\Scripts\python tools\a2dp_player.py --hci-transport usb:<path> --device-config config\device.json pair AA:BB:CC:DD:EE:FF
```

**5. Run the bridge.**

```bat
airpods_bridge.cmd usb:<path> AA:BB:CC:DD:EE:FF
```

In Windows, play to **Voicemeeter VAIO3 Input** and record from **CABLE Output**.
You can rename them in Sound settings to **AirPods Pro** and **AirPods Pro Microphone**.

## Run it at logon

```powershell
.\setup\autostart.ps1 -Install -Transport usb:<path> -AirPods AA:BB:CC:DD:EE:FF
.\setup\autostart.ps1 -Status
.\setup\autostart.ps1 -Stop
.\setup\autostart.ps1 -Start
.\setup\autostart.ps1 -Uninstall
```

After changing the code, run `-Update` with the same `-Transport` and `-AirPods` values.
Logs are in `%LOCALAPPDATA%\airpodlink\bridge.log`.

## Using it

- **Mic on demand:** the mic stream starts when an app records, and stops 3 s after the last one closes.
- **Mute:** a stem click while an app is recording, or **Ctrl+Alt+M**. Otherwise a stem click is play/pause.
- **Tray icon:** status, battery, codec, mic gain, volume sync, and Disconnect to free the AirPods for another device.

## Optional: AAC-ELD with FDK AAC

This repository works without FDK AAC. Playback uses SBC, and the mic is decoded with FFmpeg.

The original build used Apple's AAC-ELD codec for playback, encoded with Fraunhofer's FDK AAC library.
**The library isn't included.** To use it, add your own copy by following
[vendor/fdk-aac/SOURCE.txt](vendor/fdk-aac/SOURCE.txt). AAC-ELD then appears in the tray's Codec menu,
or you can run with `--codec=eld`.

- **AAC-ELD:** about the same latency as SBC, and should sound better than SBC at the AirPods' bitpool limit of 27.
- **AAC-LC:** works without the library using FFmpeg's encoder, but that sounded much worse in testing.
  With FDK it sounded much better, at about 60 ms more latency than SBC.

If you run the bridge at logon, run `-Update` after adding the file.

**Why it's left out:** FDK AAC has its own licence, which the Free Software Foundation considers incompatible
with the GPL. AAC and AAC-ELD are also patent-encumbered. If you add the library, check that your use is covered.

## Measured results

One PC, one pair of AirPods. Full method and data are in the [engineering notes](docs/engineering-notes.md).

| | Result |
|---|---|
| Mic latency, by clap test | about 189 ms |
| 7-minute mic soak | 0 underruns, 0 dropped packets, 0 decode errors |
| Round trip with SBC (default) | about 205 ms |
| Round trip with Apple AAC-ELD (optional) | about 208 ms |
| Round trip with FDK AAC-LC (optional) | about 264 ms |
| Estimated playback delay with SBC | about 130–145 ms |

## Known limitations

- The tested dongle sometimes stops responding until it's replugged. The automatic USB reset is unproven.
- Stem clicks with an iPhone nearby have once dropped the link, possibly through automatic switching.
- Occasional single-bud drops are undiagnosed.
- Windows apps can't be told about the Bluetooth delay, so video may need a manual audio offset.

## Credits

- [LibrePods](https://github.com/kavishdevar/librepods), especially [PR #655](https://github.com/kavishdevar/librepods/pull/655) by LuanAdemi, the source of the AACP mic protocol.
- [han-um's Pico 2 W port](https://github.com/han-um/USBPods-Pico2W-2-way-audio-implement), for the Apple AAC-ELD playback details.
- [Bumble](https://github.com/google/bumble), the user-mode Bluetooth stack.
- [airlow](https://github.com/jkryspin/airlow), the same WinUSB approach for AirPods Pro 2.

## Licence

GPL-3.0. See [LICENSE](LICENSE). The protocol code derives from GPL-licensed LibrePods and the Pico port.

AAC and AAC-ELD are patent-encumbered, and this repository grants no patent licence.
FDK AAC is not distributed here. Its licence text is in [vendor/fdk-aac/NOTICE](vendor/fdk-aac/NOTICE) for anyone who adds it.
