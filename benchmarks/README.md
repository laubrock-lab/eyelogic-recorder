# Benchmarks

Scripts that produce the numbers for the validation part of the paper. Install
the package first (`pip install -e .` in the repository root) and run from the
repository root. Results go to `results/` (JSON per run; not version-controlled).

| Script | Needs | Measures |
|---|---|---|
| `synthetic_throughput.py` | any OS (simulator) | Callback cost and gaze-sample delivery lag of three recording strategies (row-in-callback, chunk-in-callback, eyelogic_recorder), with and without periodic disk stalls; sample loss |
| `hardware_recording.py` | Windows + tracker (`--simulate` for a dry run) | Per frame-rate mode: missing samples, invalid fraction, inter-sample intervals, `SysTime − ETTime` and its drift, `time.time()` resolution, writer queue statistics |
| `gaze_contingent_latency.md` | photodiode setup | Protocol for display-change latency |
| `analyze.py` | | Combines result JSONs into one table and `summary.csv` |

`baselines.py` holds the two earlier in-callback strategies (reconstructed from
the repository history) used for comparison.

```
python benchmarks/synthetic_throughput.py --seconds 30
python benchmarks/hardware_recording.py --seconds 120 --calibrate
python benchmarks/analyze.py results/*.json
```

## Notes for interpretation

- In the simulator, a slow callback delays all later deliveries. How the real
  SDK behaves when the callback is slow (queue or drop) is unknown; the
  hardware run shows it through index gaps.
- On a fast SSD without stalls all strategies keep up; the asynchronous writer
  matters when writes block (antivirus, network or synced folders, slow disks).
  Report both conditions.
- `ETTime` is on the host clock (EyeLogic, 2026-10-15), so
  `SysTime − ETTime` approximates the delivery latency from acquisition to the callback. In a
  hardware test (2026-10-20) it was negative for 5–15% of samples (down to
  −13 ms) and the offset shifted within runs, so use medians, not single
  samples.
