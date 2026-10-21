"""Callback cost and delivery lag of three recording strategies, on the simulator.

Runs on any OS without a tracker. The simulator delivers samples from a thread
at a fixed rate, like the SDK. A slow callback delays all later deliveries,
so callback cost turns into gaze-sample lag (what a gaze-contingent display
sees). Optionally the disk stalls periodically (--stall-ms) to model antivirus
scans, network drives or sync clients.

Strategies:
  row_in_callback    csv.writer row per sample inside the callback
  chunk_in_callback  NumPy chunk, formatted and written inside the callback when full
  eyelogic_recorder  EyeLogicClient: callback copies into a chunk, writer thread does I/O

    python benchmarks/synthetic_throughput.py                     # 250 and 1000 Hz, with and without stalls
    python benchmarks/synthetic_throughput.py --rates 250 --seconds 30 --stall-ms 100
"""

import argparse
import json
import os
import tempfile
import time
from datetime import datetime

import numpy as np

from eyelogic_recorder import EyeLogicClient, simulator
from eyelogic_recorder.io import load_samples

from analyze import summarize_timing, timer_resolution
from baselines import ChunkInCallback, RowInCallback, StallingFile


def run_baseline(cls, rate, seconds, stall_ms, stall_every, folder):
    mod = simulator.install(rate_hz=rate, record_timing=True)
    api = mod.ELApi("benchmark")
    path = os.path.join(folder, "{}_{}Hz_{}ms.csv".format(cls.name, rate, stall_ms))
    raw = open(path, "w", newline="", encoding="utf-8")
    file = StallingFile(raw, stall_ms / 1000, stall_every)
    recorder = cls(file, mod)
    callback = mod.GazeSampleCallback(recorder.callback)
    api.registerGazeSampleCallback(callback)
    api.requestTracking(0)
    time.sleep(seconds)
    api.unrequestTracking()
    recorder.close()
    raw.close()
    return path, api.timing, file.stalls


def run_eyelogic_recorder(rate, seconds, stall_ms, stall_every, folder):
    simulator.install(rate_hz=rate, record_timing=True)
    with EyeLogicClient("eyelogic_recorder_{}Hz_{}ms".format(rate, stall_ms), data_folder=folder) as et:
        et._sample_file = StallingFile(et._sample_file, stall_ms / 1000, stall_every)  # benchmark-only hook
        et.connect()
        et.start_recording()  # before tracking starts, so every delivered sample is recorded
        et.start_tracking()
        time.sleep(seconds)
        et.stop_tracking()
        stats = {"samples_dropped": et.samples_dropped, "max_queue_depth": et.max_queue_depth}
        return et.sample_file.name, et.api.timing, et.sample_file.stalls, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rates", type=float, nargs="+", default=[250, 1000])
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--stall-ms", type=float, nargs="+", default=[0, 50], help="disk stall durations to test")
    parser.add_argument("--stall-every", type=float, default=1.0, help="seconds between stalls")
    parser.add_argument("--out", default="results", help="output directory for the JSON result")
    parser.add_argument("--keep-data", action="store_true", help="keep the recorded CSV files")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="eyelogic_recorder_bench_") if not args.keep_data else os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)
    run_eyelogic_recorder(args.rates[0], 1.0, 0, args.stall_every, data_dir)  # warm-up (imports, page cache), discarded
    runs = []
    for rate in args.rates:
        for stall_ms in args.stall_ms:
            for strategy in ("row_in_callback", "chunk_in_callback", "eyelogic_recorder"):
                extra = {}
                if strategy == "eyelogic_recorder":
                    path, timing, stalls, extra = run_eyelogic_recorder(rate, args.seconds, stall_ms, args.stall_every, data_dir)
                else:
                    cls = RowInCallback if strategy == "row_in_callback" else ChunkInCallback
                    path, timing, stalls = run_baseline(cls, rate, args.seconds, stall_ms, args.stall_every, data_dir)
                data = load_samples(path)
                idx = data["index"].astype(np.int64)
                row = {
                    "benchmark": "synthetic",
                    "strategy": strategy,
                    "rate_hz": rate,
                    "stall_ms": stall_ms,
                    "stalls": stalls,
                    "rows_written": int(data.size),
                    # samples absent between the first and last recorded one (true losses)
                    "lost_within_recording": int(idx[-1] - idx[0] + 1 - data.size) + int(idx[0]),
                    # delivered after recording stopped (by design not recorded; not a loss)
                    "delivered_after_stop": int(len(timing) - 1 - idx[-1]),
                }
                row.update(summarize_timing(timing, 1 / rate))
                row.update(extra)
                runs.append(row)
                print("{strategy:18s} {rate_hz:6.0f} Hz  stall {stall_ms:4.0f} ms  "
                      "callback p99 {callback_us_p99:8.1f} us  lag p99 {lag_ms_p99:7.2f} ms  "
                      "lag max {lag_ms_max:7.2f} ms  lost {lost_within_recording}".format(**row), flush=True)

    result = {
        "benchmark": "synthetic_throughput",
        "created": datetime.now().isoformat(timespec="seconds"),
        "platform": __import__("platform").platform(),
        "python": __import__("platform").python_version(),
        "time_time_resolution_ms": timer_resolution() * 1000,
        "args": vars(args),
        "runs": runs,
    }
    out = os.path.join(args.out, "synthetic_{:%Y%m%d_%H%M%S}.json".format(datetime.now()))
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print("wrote", out)


if __name__ == "__main__":
    main()
