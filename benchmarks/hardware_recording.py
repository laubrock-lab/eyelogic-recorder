"""Recording quality on real hardware, per frame-rate mode (Windows lab PC with tracker).

For each frame-rate mode, records N seconds (with a message every second) and
reports: samples, index gaps / missing samples, invalid fraction, inter-sample
intervals from the tracker timestamps, SysTime - ETTime (arrival minus
SDK timestamp, approximately the delivery latency; see benchmarks/README.md), its drift, the
resolution of time.time(), and the writer's queue statistics.

    python benchmarks/hardware_recording.py --seconds 60                 # all frame-rate modes
    python benchmarks/hardware_recording.py --modes 2 --seconds 300 --calibrate
    python benchmarks/hardware_recording.py --simulate --seconds 5       # dry run without tracker

Look at the screen or keep a participant (or artificial eye) in the tracker's
box during recording; otherwise most samples are invalid.
"""

import argparse
import json
import os
import platform
import time
from datetime import datetime

from eyelogic_recorder import EyeLogicClient, EyeLogicError, simulator

from analyze import summarize_samples, timer_resolution


def record_mode(mode, seconds, folder, calibrate):
    with EyeLogicClient("hw_mode{}".format(mode), data_folder=folder) as et:
        et.connect()
        et.find_tracker()
        rates = list(et.api.getDeviceConfig().frameRates)
        et.start_tracking(mode)
        if calibrate:
            et.calibrate()
            et.validate()
        et.start_recording()
        try:
            t_end = time.time() + seconds
            n = 0
            while time.time() < t_end:
                n += 1
                et.send_message("TICK {}".format(n))
                time.sleep(1.0)
                et.check_health()
        finally:
            et.stop_recording()
        stats = {
            "frame_rate_mode": mode,
            "nominal_rate_hz": rates[mode] if mode < len(rates) else None,
            "samples_written": et.samples_written,
            "samples_dropped": et.samples_dropped,
            "max_queue_depth": et.max_queue_depth,
        }
        return et.sample_file.name, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modes", type=int, nargs="*", help="frame-rate mode indices (default: all)")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--calibrate", action="store_true", help="calibrate and validate before each mode")
    parser.add_argument("--simulate", action="store_true", help="use eyelogic_recorder.simulator instead of a tracker")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    if args.simulate:
        simulator.install()
    os.makedirs(args.out, exist_ok=True)
    folder = os.path.join(args.out, "hardware_data")

    modes = args.modes
    if not modes:
        with EyeLogicClient("probe", data_folder=folder) as et:
            et.connect()
            et.find_tracker()
            modes = list(range(len(et.api.getDeviceConfig().frameRates)))

    runs = []
    for mode in modes:
        try:
            path, stats = record_mode(mode, args.seconds, folder, args.calibrate)
        except EyeLogicError as err:
            print("mode {}: {}".format(mode, err))
            runs.append({"benchmark": "hardware", "frame_rate_mode": mode, "error": str(err)})
            continue
        row = {"benchmark": "hardware", "simulated": args.simulate}
        row.update(stats)
        row.update(summarize_samples(path))
        runs.append(row)
        print("mode {frame_rate_mode} ({nominal_rate_hz} Hz): {rows} rows, rate {rate_hz:.1f} Hz, "
              "missing {missing_samples}, invalid {invalid_fraction:.1%}, "
              "SysTime-ETTime median {systime_minus_ettime_ms_p50:.2f} ms".format(**row), flush=True)

    result = {
        "benchmark": "hardware_recording",
        "created": datetime.now().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "time_time_resolution_ms": timer_resolution() * 1000,
        "args": vars(args),
        "runs": runs,
    }
    out = os.path.join(args.out, "hardware_{:%Y%m%d_%H%M%S}.json".format(datetime.now()))
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print("time.time() resolution: {:.4f} ms".format(result["time_time_resolution_ms"]))
    print("wrote", out)


if __name__ == "__main__":
    main()
