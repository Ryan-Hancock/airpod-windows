"""Replay a phase-2 SDU capture through MicPipeline into VB-Cable at real-time pacing.

Usage: tools/winpy.sh tools/replay_capture.py captures/mic-XXXX.sdu [--gain-db=G]
Records "CABLE Output" meanwhile (captures/replay-*.wav) to check the whole Windows path
without the AirPods.
"""
import pathlib
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from airpodlink.mic_pipeline import MicPipeline  # noqa: E402
from tools.mic_bridge import CableRecorder  # noqa: E402


def main(path, gain_db):
    data = pathlib.Path(path).read_bytes()
    records, off = [], 0
    while off + 10 <= len(data):
        t_ns, n = struct.unpack_from("<QH", data, off)
        records.append((t_ns, data[off + 10:off + 10 + n]))
        off += 10 + n
    pipeline = MicPipeline(gain_db=gain_db)
    print("output:", pipeline.start())
    pipeline.live = True
    out = pathlib.Path(path).with_name(pathlib.Path(path).stem.replace("mic-", "replay-") + ".wav")
    with CableRecorder(out):
        t0_rec, t0 = records[0][0], time.perf_counter()
        for i, (t_ns, sdu) in enumerate(records):
            delay = (t_ns - t0_rec) / 1e9 - (time.perf_counter() - t0)
            if delay > 0:
                time.sleep(delay)          # reproduce the original arrival timing
            pipeline.feed_sdu(sdu)
            if i and i % 33 == 0:
                print("MIC:", pipeline.report())
        time.sleep(0.3)
    print("MIC:", pipeline.report())
    pipeline.stop()


if __name__ == "__main__":
    g = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--gain-db=")), 0.0)
    main(sys.argv[1], g)
