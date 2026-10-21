"""Summaries of recorded sample files and benchmark results.

    python benchmarks/analyze.py results/*.json      # print tables, write results/summary.csv
"""

import argparse
import csv
import json
import os
import time

import numpy as np

from eyelogic_recorder.io import load_samples


def _pct(values, qs=(50, 99, 100)):
    if len(values) == 0:
        return {q: float("nan") for q in qs}
    return {q: float(np.percentile(values, q)) for q in qs}


def summarize_samples(path):
    """Data-quality summary of one sample CSV (as written by EyeLogicClient)."""
    d = load_samples(path)
    out = {"file": os.path.basename(path), "rows": int(d.size)}
    if d.size < 2:
        return out
    idx = d["index"].astype(np.int64)
    step = np.diff(idx)
    out["index_gaps"] = int((step != 1).sum())
    out["missing_samples"] = int((step[step > 1] - 1).sum())
    out["invalid_fraction"] = float(np.isnan(d["porRawX"]).mean())

    et_s = d["timestampMicroSec"].astype(np.int64) / 1e6
    isi_ms = np.diff(et_s)[step == 1] * 1000
    out["isi_ms_mean"] = float(isi_ms.mean())
    out["isi_ms_sd"] = float(isi_ms.std())
    out["isi_ms_min"] = float(isi_ms.min())
    out["isi_ms_max"] = float(isi_ms.max())
    out["rate_hz"] = float(1000 / np.median(isi_ms))

    # Arrival minus SDK timestamp, approximately the delivery latency (see benchmarks/README.md).
    offset_ms = (d["sysTime"] - et_s) * 1000
    q = _pct(offset_ms, (1, 25, 50, 75, 99))
    out.update({"systime_minus_ettime_ms_p{}".format(k): v for k, v in q.items()})
    minutes = (et_s - et_s[0]) / 60
    out["offset_drift_ms_per_min"] = float(np.polyfit(minutes, offset_ms, 1)[0]) if minutes[-1] > 0 else float("nan")

    sys_steps = np.diff(np.unique(d["sysTime"]))
    out["systime_min_step_ms"] = float(sys_steps.min() * 1000) if sys_steps.size else float("nan")
    return out


def summarize_timing(timing, period_s):
    """Summary of simulator delivery timing: rows of (nominal_s, call_time_s, callback_duration_s)."""
    t = np.asarray(timing, dtype=float)
    lag_ms = (t[:, 1] - t[:, 0]) * 1000
    dur_us = t[:, 2] * 1e6
    lag = _pct(lag_ms)
    dur = _pct(dur_us)
    return {
        "delivered": int(len(t)),
        "callback_us_p50": dur[50], "callback_us_p99": dur[99], "callback_us_max": dur[100],
        "lag_ms_p50": lag[50], "lag_ms_p99": lag[99], "lag_ms_max": lag[100],
        "late_by_more_than_one_period": int((lag_ms > period_s * 1000).sum()),
    }


def timer_resolution(duration_s=0.2):
    """Smallest positive step of ``time.time()`` observed in a tight loop (seconds)."""
    end = time.perf_counter() + duration_s
    smallest = float("inf")
    last = time.time()
    while time.perf_counter() < end:
        now = time.time()
        if now != last:
            smallest = min(smallest, now - last)
            last = now
    return smallest


def _print_table(rows, keys):
    widths = [max(len(k), *(len(_fmt(r.get(k))) for r in rows)) for k in keys]
    print("  ".join(k.ljust(w) for k, w in zip(keys, widths)))
    for r in rows:
        print("  ".join(_fmt(r.get(k)).ljust(w) for k, w in zip(keys, widths)))


def _fmt(v):
    if isinstance(v, float):
        return "{:.3g}".format(v)
    return "" if v is None else str(v)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", help="JSON files written by the benchmark scripts")
    parser.add_argument("--out", help="summary CSV (default: summary.csv next to the first input)")
    args = parser.parse_args()

    rows = []
    for path in args.results:
        with open(path) as f:
            rows.extend(json.load(f)["runs"])
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("benchmark", "strategy", "rate_hz", "stall_ms"), k))
    _print_table(rows, keys)
    out = args.out or os.path.join(os.path.dirname(args.results[0]), "summary.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print("\nwrote", out)


if __name__ == "__main__":
    main()
