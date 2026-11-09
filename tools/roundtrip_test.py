"""Playback-latency A/B via the AirPods' own mic (round trip on one clock).

Plays short 3 kHz bursts into the AirPods playback endpoint while the bridge streams it to
the AirPods and their hi-res mic streams back. Both ends are timestamped in THIS process with
perf_counter():
  t_out = when the burst arrived in our WASAPI loopback capture (start of our A2DP pipeline)
  t_in  = when the burst came back in a mic SDU (AirPods speaker -> AirPods mic -> radio)
round trip = t_in - t_out = A2DP path (encoder, radio, AirPods playback buffer) + acoustic
+ AirPods mic path (ELD encode, 4-AU/30 ms packing, radio). The mic part is the same for
every playback configuration, so DIFFERENCES between configurations are exact.

Stop the service first (setup/autostart.ps1 -Stop). Wear both AirPods; quiet room.
Usage: tools/winpy.sh tools/roundtrip_test.py usb:1-2.4.2 AA:BB:CC:DD:EE:FF [--codec=aac|sbc|eld] [--eld-frames=N]
         [--aac-encoder=fdk|ffmpeg] [--sbc-frames=N] [--flushable-ms=N] [--bursts=8] [--volume=60]
"""
import asyncio
import json
import logging
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from airpodlink.a2dp_stream import A2dpStream  # noqa: E402
from airpodlink.aacp import demux_audio_sdu  # noqa: E402
from airpodlink.devices import PLAYBACK, pick, wasapi_names  # noqa: E402
from airpodlink.eld import ELD_STREAM_RATE, EldDecoder  # noqa: E402
from airpodlink.link import ROOT, connect_airpods, disconnect, open_device, say  # noqa: E402
from airpodlink.link_monitor import enable_flushable_media  # noqa: E402
from airpodlink.live_source import LiveSource  # noqa: E402
from airpodlink.mic_pipeline import MicPipeline  # noqa: E402
from airpodlink.mic_session import MicSession  # noqa: E402

logging.basicConfig(level=os.environ.get("BUMBLE_LOGLEVEL", "WARNING").upper())
SPACING = 1.5
BURST_HZ, BURST_MS = 3000, 20


def burst(rate):
    t = np.arange(int(rate * BURST_MS / 1000)) / rate
    return (np.sin(2 * np.pi * BURST_HZ * t) * np.hanning(t.size)).astype(np.float32)


def timeline(chunks):
    """chunks: [(arrival, samples_at_rate)] -> (signal, offset) where sample i was captured
    at ~ offset + i/rate. Lower envelope: a sample can't arrive before it was captured."""
    sig = np.concatenate([c for _, c in chunks])
    ends = np.cumsum([len(c) for _, c in chunks])
    arrivals = np.array([a for a, _ in chunks])
    return sig, arrivals, ends


def detect(sig, rate, count, min_gap_s=1.0):
    tpl = burst(rate)
    corr = np.abs(np.correlate(sig, tpl, mode="valid"))
    floor = np.median(corr) + 1e-9
    found = []
    c = corr.copy()
    for _ in range(count):
        i = int(np.argmax(c))
        if c[i] < 8 * floor:
            break
        found.append((i, float(c[i] / floor)))
        c[max(0, i - int(min_gap_s * rate)): i + int(min_gap_s * rate)] = 0
    return sorted(found)


async def main(spec, address, o):
    pipeline = MicPipeline()
    pipeline.start()
    cap_chunks, sdu_log = [], []
    async with open_device(spec) as (device, avrcp_protocol):
        connection = await connect_airpods(device, address)
        source = LiveSource(codec=o["codec"], aac_encoder=o["aac_encoder"], sbc_frames_per_packet=o["sbc_frames"],
                            eld_frames_per_packet=o["eld_frames"])
        original_cap = source._on_capture

        def tap_capture(indata, frames, t, status, rate=48000):
            if o["recording"]:
                cap_chunks.append((time.perf_counter(), indata[:, 0].copy(), rate))
            return original_cap(indata, frames, t, status, rate)

        source._on_capture = tap_capture
        stream = A2dpStream(connection, avrcp_protocol, source, o["volume"])
        await stream.start()
        if o["flush_ms"]:
            say(await enable_flushable_media(device.host, connection, stream.stream.rtp_channel, o["flush_ms"]))
        mic = MicSession(connection, avrcp_protocol.delegate, pipeline, hotkey=False)
        original_sdu = mic._on_sdu

        def tap_sdu(sdu):
            if o["recording"]:
                sdu_log.append((time.perf_counter(), sdu))
            original_sdu(sdu)

        mic._on_sdu = tap_sdu
        mic.aacp.on_audio_sdu = tap_sdu
        await mic.start(mic=True)
        await asyncio.sleep(3.0)  # let both streams settle

        name = pick(PLAYBACK, wasapi_names("output"))
        wasapi = next(i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"])
        idx = next(i for i, d in enumerate(sd.query_devices()) if d["hostapi"] == wasapi and d["name"] == name)
        rate = int(sd.query_devices(idx)["default_samplerate"])
        sig = np.zeros(int(rate * SPACING * (o["bursts"] + 1)), np.float32)
        b = 0.6 * burst(rate)
        for k in range(o["bursts"]):
            s0 = int(rate * (0.5 + k * SPACING))
            sig[s0:s0 + b.size] = b
        say(f"playing {o['bursts']} bursts via '{name}' ({source})")
        o["recording"] = True
        sd.play(np.stack([sig, sig], 1), rate, device=idx, blocking=False)
        await asyncio.sleep(len(sig) / rate + 1.0)
        o["recording"] = False
        sd.stop()
        await mic.stop()
        await stream.close()
        await disconnect(connection)
    pipeline.stop()

    # --- outbound: loopback capture
    cap_rate = cap_chunks[0][2]
    out_sig, out_arr, out_ends = timeline([(a, c) for a, c, _ in cap_chunks])
    out_off = np.min(out_arr - out_ends / cap_rate)
    out_hits = detect(out_sig, cap_rate, o["bursts"])
    t_out = [out_off + i / cap_rate for i, _ in out_hits]
    # --- inbound: decode mic SDUs (4 x 480 samples @ 64 kHz each)
    dec = EldDecoder()
    in_chunks = []
    for arrival, sdu in sdu_log:
        aus, _ = demux_audio_sdu(sdu)
        pcm = np.concatenate([dec.decode(au) for _, au in aus]) if aus else np.zeros(0, np.float32)
        in_chunks.append((arrival, pcm))
    in_sig, in_arr, in_ends = timeline(in_chunks)
    in_off = np.min(in_arr - in_ends / ELD_STREAM_RATE)
    in_hits = detect(in_sig, ELD_STREAM_RATE, o["bursts"] * 2)
    t_in = [in_off + i / ELD_STREAM_RATE for i, _ in in_hits]

    rt = []
    for t in t_out:
        c = [u - t for u in t_in if 0.02 < u - t < 1.0]
        if c:
            rt.append(min(c) * 1000)
    snr = [round(20 * np.log10(s), 1) for _, s in in_hits]
    label = (f"{o['codec']}" + {"aac": f"/{o['aac_encoder']}", "sbc": f"/{o['sbc_frames']}fpp",
                                 "eld": f"/{o['eld_frames']}fpp"}[o["codec"]]
             + (f"/flush{o['flush_ms']:.0f}" if o["flush_ms"] else ""))
    if rt:
        r = np.array(rt)
        print(f"ROUNDTRIP {label}: n={len(r)}/{o['bursts']} median {np.median(r):.1f} ms "
              f"min {r.min():.1f} max {r.max():.1f} stdev {r.std():.1f}  (mic-side burst SNR dB {snr})")
    else:
        print(f"ROUNDTRIP {label}: no bursts matched; outbound found {len(t_out)}, mic found {len(t_in)} "
              f"(SNR dB {snr}) - the mic may not hear the AirPods' own speaker at this volume")
    with open(ROOT / "captures" / "roundtrip.jsonl", "a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "config": label,
                            "ms": [round(v, 1) for v in rt]}) + "\n")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kv = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    asyncio.run(main(args[0], args[1], {
        "codec": kv.get("codec", "aac"), "aac_encoder": kv.get("aac-encoder", "fdk"),
        "sbc_frames": int(kv.get("sbc-frames", 5)), "eld_frames": int(kv.get("eld-frames", 1)), "flush_ms": float(kv.get("flushable-ms", 100)) or None,
        "bursts": int(kv.get("bursts", 8)), "volume": int(kv.get("volume", 60)), "recording": False,
    }))
