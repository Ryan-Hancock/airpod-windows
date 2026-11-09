"""Phase 5 latency harness. Run while airpods_bridge.py is running.

Uses a Windows microphone near the AirPods (default: the NexiGo webcam mic) as a neutral
acoustic reference. All captures run in this one process via PyAudioWPatch and are put on a
single timeline by stamping each 5 ms buffer with perf_counter() on arrival (minus its
duration) and smoothing with a linear fit over the run. (PortAudio's ADC timestamps are not
comparable between WASAPI loopback and capture streams; see the self-test note in cb().)

  playback : plays N tone bursts into the AirPods playback endpoint ("Voicemeeter VAIO3 Input")
             and captures (a) that endpoint's WASAPI loopback = when Windows emitted the sound,
             (b) the reference mic = when it came out of an AirPod held next to it.
             latency = matched-filter peak(b) - peak(a)    [Windows mixer -> air at the AirPod]
  mic      : you clap/tap near both the AirPods and the reference mic; captures (a) the
             reference mic and (b) "CABLE Output" (the bridge's mic output).
             latency = cross-correlation lag of (b) vs (a)   [air -> what Windows apps record]

Error budget: the reference mic's own input latency is not perfectly known (USB mic, WASAPI
reports it; we use its ADC timestamps) - expect roughly +-5..10 ms absolute; comparisons
between configurations on the same setup are more precise than absolute numbers.

Usage: tools/winpy.sh tools/latency_test.py playback|mic [--count=10] [--ref="NexiGo"]
Self-tests of the harness (no AirPods; bridge must NOT be running):
       ... playback --play="CABLE Input" --ref="loopback:CABLE Input"   (true latency 0: harness error)
       ... playback --play="CABLE Input" --ref="CABLE Output"           (VB-Cable's own latency)
"""
import json
import pathlib
import sys
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

RATE = 48000
ROOT = pathlib.Path(__file__).resolve().parents[1]


class Capture:
    """One input (or loopback) stream recorded with a timeline of (sample_index, time)."""

    def __init__(self, pa, info, label):
        self.label = label
        self.channels = info["maxInputChannels"]
        self.rate = int(info["defaultSampleRate"])
        self.chunks, self.marks = [], []
        self.n = 0
        self.adc_valid = True
        self.lock = threading.Lock()

        def cb(data, frames, time_info, status):
            x = np.frombuffer(data, np.float32).reshape(frames, self.channels).mean(axis=1)
            # Arrival-time stamping (same meaning for loopback and capture streams). PortAudio's
            # ADC times are not comparable across them: loopback subtracts a different
            # "latency" (self-test showed CABLE Output ~15 ms *before* its own loopback source).
            arrival = time.perf_counter()
            self.adc_valid = False
            with self.lock:
                self.chunks.append(x.copy())
                self.n += frames
                self.marks.append((self.n, arrival))  # last sample of this block arrived at `arrival`
            return (None, pyaudio.paContinue)

        self.stream = pa.open(format=pyaudio.paFloat32, channels=self.channels, rate=self.rate,
                              input=True, input_device_index=info["index"],
                              frames_per_buffer=int(self.rate * 0.005), stream_callback=cb)

    def start(self):
        self.stream.start_stream()

    def stop(self):
        self.stream.stop_stream()
        self.stream.close()

    def signal(self):
        x = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
        if self.rate != RATE:
            idx = np.arange(0, len(x) * RATE / self.rate) * self.rate / RATE
            x = np.interp(idx, np.arange(len(x)), x).astype(np.float32)
        return x

    def time_of(self, sample_at_48k: float) -> float:
        s = sample_at_48k * self.rate / RATE
        idx, t = np.array(self.marks).T
        # Lower envelope: a sample can never arrive before it was captured, so the tightest
        # bound on (arrival - sample/rate) is the stream's minimum delivery delay. Startup
        # backlog bursts only ever add delay, so they don't move the minimum.
        offset = np.min(t - idx / self.rate)
        return s / self.rate + offset


def find(pa, name, loopback=False):
    devs = pa.get_loopback_device_info_generator() if loopback else (
        pa.get_device_info_by_index(i) for i in range(pa.get_device_count()))
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    for d in devs:
        if d["hostApi"] == wasapi and d["maxInputChannels"] > 0 and name.lower() in d["name"].lower():
            return d
    raise SystemExit(f"device {name!r} ({'loopback' if loopback else 'input'}) not found")


def burst(freq=3000, ms=15):
    t = np.arange(int(RATE * ms / 1000)) / RATE
    return (np.sin(2 * np.pi * freq * t) * np.hanning(t.size)).astype(np.float32)


def peaks(x, template, count, spacing):
    corr = np.abs(np.correlate(x, template, mode="valid"))
    found = []
    for _ in range(count * 2):
        i = int(np.argmax(corr))
        if corr[i] <= 0:
            break
        found.append((i, float(corr[i])))
        corr[max(0, i - spacing // 2): i + spacing // 2] = 0
    found.sort()
    return found


def playback_test(count, ref_name, play_name=None):
    if play_name is None:
        from airpodlink.devices import PLAYBACK, pick, wasapi_names
        play_name = pick(PLAYBACK, wasapi_names("output"))
    pa = pyaudio.PyAudio()
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    out = next(d for d in (pa.get_device_info_by_index(i) for i in range(pa.get_device_count()))
               if d["hostApi"] == wasapi and d["maxOutputChannels"] > 0 and play_name in d["name"])
    loop = Capture(pa, find(pa, play_name, loopback=True), "loopback")
    ref_loop = ref_name.startswith("loopback:")
    ref = Capture(pa, find(pa, ref_name.split(":", 1)[-1], loopback=ref_loop),
                  "reference (loopback)" if ref_loop else "reference mic")
    spacing = int(RATE * 1.2)
    tpl = burst()
    sig = np.zeros(spacing * (count + 1), np.float32)
    for k in range(count):
        sig[spacing // 2 + k * spacing: spacing // 2 + k * spacing + tpl.size] = 0.5 * tpl
    # Keep the render endpoint busy (silence first) for the whole run: WASAPI loopback can
    # deliver nothing while an endpoint is idle, which would desync its sample timeline.
    lead = np.zeros(RATE, np.float32)
    stereo = np.repeat(np.concatenate([lead, sig, lead])[:, None], 2, axis=1)
    pos = [0]

    def play_cb(_in, frames, _ti, _st):
        chunk = stereo[pos[0]:pos[0] + frames]
        pos[0] += frames
        if len(chunk) < frames:
            chunk = np.vstack([chunk, np.zeros((frames - len(chunk), 2), np.float32)])
        return (chunk.tobytes(), pyaudio.paContinue)

    player = pa.open(format=pyaudio.paFloat32, channels=2, rate=RATE, output=True,
                     output_device_index=out["index"], frames_per_buffer=240, stream_callback=play_cb)
    player.start_stream()
    time.sleep(0.3)
    loop.start(); ref.start()
    while pos[0] < len(stereo):
        time.sleep(0.1)
    time.sleep(0.3)
    loop.stop(); ref.stop(); player.stop_stream(); player.close()
    save_raw("playback", loop, ref)
    a, b = loop.signal(), ref.signal()
    pa_ = peaks(a, tpl, count, spacing)
    pb_ = peaks(b, tpl, count, spacing)
    ta = [loop.time_of(i) for i, _ in pa_]
    tb = [ref.time_of(i) for i, _ in pb_]
    if "--debug" in sys.argv:
        for c, pk, ts in ((loop, pa_, ta), (ref, pb_, tb)):
            m = np.array(c.marks)
            d = (m[:, 1] - m[:, 0] / c.rate) * 1000
            d -= d.min()
            q = np.percentile(d, [0, 10, 50, 90, 100])
            slope = np.polyfit(m[:, 0] / c.rate, m[:, 1], 1)[0]
            print(f"  {c.label}: {c.n} samples @ {c.rate}, wall/audio slope {slope:.5f}, "
                  f"delivery delay above min (ms) p0/p10/p50/p90/max = {np.round(q, 1).tolist()}, "
                  f"first 6 = {np.round(d[:6], 1).tolist()}")
            print(f"    peaks (sample, strength): {[(i, round(v, 2)) for i, v in pk[:4]]} ...")
            print(f"    times: {[round(t, 4) for t in ts[:4]]} ...")
    lat = []
    for t in ta:  # pair each emitted burst with the first reference arrival -0.1..1 s later
        cands = [u - t for u in tb if -0.1 <= u - t < 1.0]  # small negatives = timeline uncertainty
        if cands:
            lat.append(min(cands) * 1000)
    report("playback (Windows mixer -> AirPod speaker)", lat, count, loop, ref)
    pa.terminate()
    return lat


def mic_test(count, ref_name):
    pa = pyaudio.PyAudio()
    ref = Capture(pa, find(pa, ref_name), "reference mic")
    from airpodlink.devices import MIC, pick, wasapi_names
    cable = Capture(pa, find(pa, pick(MIC, wasapi_names("input"))), "CABLE Output")
    secs = count * 1.5 + 3
    print(f"Recording {secs:.0f} s: CLAP (or tap the desk sharply) {count} times, ~1.5 s apart, "
          "with the AirPods in your ears and the reference mic near you...", flush=True)
    ref.start(); cable.start()
    time.sleep(secs)
    ref.stop(); cable.stop()
    save_raw("mic", ref, cable)
    a, b = ref.signal(), cable.signal()
    lat = mic_latencies(a, b, ref, cable)
    report("mic (air at AirPods -> CABLE Output)", lat, count, ref, cable)
    pa.terminate()
    return lat


def save_raw(tag, *caps):
    """Keep raw recordings + timelines for offline re-analysis (captures/latency-*.npz)."""
    out = ROOT / "captures" / time.strftime(f"latency-{tag}-%Y%m%d-%H%M%S.npz")
    np.savez_compressed(out, **{f"{c.label}_signal": c.signal() for c in caps},
                        **{f"{c.label}_marks": np.array(c.marks) for c in caps},
                        **{f"{c.label}_rate": c.rate for c in caps})
    print(f"raw saved: {out.name}")


def onsets(x, min_gap_s=0.6, k=10.0):
    """Clap onsets: envelope rises above k x noise floor; onset = first sample above 25% of
    that event's local peak (robust to different mic sensitivities/gating)."""
    env = np.convolve(np.abs(x), np.ones(48) / 48, mode="same")  # 1 ms
    nz = env[env > 0]
    floor = np.percentile(nz, 20) if nz.size else 1e-6
    out, i = [], 0
    while i < len(env):
        if env[i] > k * floor:
            w = env[i: i + int(0.05 * RATE)]
            pk = w.max()
            j0 = max(0, i - int(0.02 * RATE))
            on = j0 + int(np.argmax(env[j0: i + len(w)] > 0.25 * pk))
            out.append(on)
            i += int(min_gap_s * RATE)
        else:
            i += 1
    return out


def mic_latencies(a, b, ref, cable):
    ta = [ref.time_of(i) for i in onsets(a)]
    tb = [cable.time_of(i) for i in onsets(b)]
    lat = []
    for t in ta:  # the AirPods path is later; take the first CABLE onset within 0..600 ms
        c = [u - t for u in tb if -0.02 <= u - t < 0.6]
        if c:
            lat.append(min(c) * 1000)
    print(f"  onsets: reference {len(ta)}, CABLE {len(tb)}, paired {len(lat)}")
    return lat


def report(name, lat, expected, *caps):
    clocks = ", ".join(f"{c.label}: {'ADC timestamps' if c.adc_valid else 'arrival-time clock'}" for c in caps)
    if not lat:
        print(f"{name}: no events matched ({clocks})")
        return
    lat = np.array(lat)
    print(f"{name}: n={len(lat)}/{expected}  median {np.median(lat):.1f} ms  "
          f"min {lat.min():.1f}  max {lat.max():.1f}  stdev {lat.std():.1f}   [{clocks}]")
    with open(ROOT / "captures" / "latency.jsonl", "a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "test": name,
                            "ms": [round(v, 1) for v in lat.tolist()]}) + "\n")


if __name__ == "__main__":
    mode = sys.argv[1]
    opts = dict(a[2:].split("=", 1) for a in sys.argv[2:] if a.startswith("--") and "=" in a)
    count = int(opts.get("count", 10))
    ref_name = opts.get("ref", "NexiGo")
    if mode == "playback":
        playback_test(count, ref_name, opts.get("play"))
    else:
        mic_test(count, ref_name)
