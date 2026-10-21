# Gaze-contingent latency: measurement protocol

Not a script: two of the measurements need extra hardware. The protocol splits
the end-to-end latency of a gaze-contingent display change into parts and
measures them with three methods, so each part can be reported on its own and
the parts can be checked against the total.

```text
eye moves ─▶ camera frame ─▶ sample computed ─▶ sample arrives ─▶ criterion met ─▶ flip() returns ─▶ photons
└──────────────── A: tracker ──────────────────┘└─ D: detection ─┘└── B: script ──┘└── C: display ──┘
```

- **A, tracker:** from the eye movement to the arrival of the first sample that
  shows it in Python (camera exposure, gaze computation, transfer).
- **D, detection:** from that sample to the sample that triggers the change. A
  boundary test on the filtered gaze position costs about one sample; criteria
  on a filtered velocity can cost several.
- **B, script:** from the arrival of the triggering sample to the return of
  `win.flip()`.
- **C, display:** from the return of `flip()` to the visible change (driver,
  compositor, waiting for the refresh, monitor input lag, panel response).

| Method | Measures | Extra hardware |
|---|---|---|
| 1. Recording | D, B | none |
| 2. Photodiode | B + C | photodiode, USB data acquisition |
| 3. High-speed camera | A + B + C (full loop) | camera with a real high frame rate, mirror |

## 1. From the recording (D and B)

Uses the boundary demo (`examples/boundary.py`), which logs
`DISPLAY_CHANGE <index>` from inside `flip()` (`win.callOnFlip`), with the
index of the sample that triggered the change.

- D = index of the triggering sample − index of the first sample whose raw
  position (`x`) is past the boundary, times the sample interval.
- B = `SysTime` of the `DISPLAY_CHANGE` message − `SysTime` of the triggering
  sample. Both are host `time.time()`, so use `SysTime`, not `ETTime`.
- On the lab PC (2026-10-20), B was 0–1 ms: `flip()` returned before the
  refresh, so the message marks the request to swap buffers, and the wait for the
  refresh falls into C. D was one sample: the filtered position crossed the
  boundary one sample after the raw position.
- Check the `time.time()` resolution in the hardware result JSON. If it is coarse
  (around 15 ms on some Windows/Python combinations), B cannot be measured.

## 2. Photodiode (B + C)

Together with the display change, the script draws a small patch white; a
photodiode on the patch is read through a USB data-acquisition device (or a
microcontroller sending a byte on each change) by a separate thread or process,
which timestamps each change with `time.time()`, the clock of `SysTime`.

B + C per trial = photodiode change − `SysTime` of the triggering sample.

- Resolution is about 1 ms (USB polling), enough for latencies in the tens of
  milliseconds.
- The monitor refreshes from top to bottom, so C depends on the vertical
  position: put the patch at the height of the text line, or report its position.
- Depends on monitor (refresh rate, input lag, panel response), display mode
  (full screen, vsync) and graphics driver. Report all of them.

## 3. High-speed camera (A + B + C)

Suggested by EyeLogic. `examples/gaze_contingent.py` shows the gaze position as a
red dot. Film the screen at a high frame rate, with a mirror placed so that the
participant's eyes are in the same video. The participant makes sudden saccades
a few times; for each, count the frames between the start of the eye movement
and the start of the dot movement.

- Use a camera with a *real* high frame rate. Phone slow-motion modes advertised
  at 960 fps record only fractions of a second or interpolate frames from 240 or
  480 fps; interpolated frames blur exactly the timing to be measured. At 240 fps
  one frame is 4.2 ms, so measure many saccades and report the median.
- Judge the start of the eye and the dot movement with the same criterion (for
  example the first frame with visible displacement).
- The dot follows the filtered gaze position and is redrawn every refresh, so
  this loop has no detection criterion beyond the filter (D of about one sample).

Alternatives without a participant: Saunders and Woods (2014) blind the tracker
briefly with an infrared LED and film the LED and the display change with a
high-speed camera. Anderson, Cooper and Otero-Millan (2026) render a simulated
pupil that the tracker follows and need no extra equipment; whether this works
with an EyeLogic tracker would have to be tested.

## Combining the methods

Run the photodiode and the camera measurement with the same script, monitor and
settings. Then the tracker part is

A ≈ median(A + B + C) − median(B + C),

and the latency of a boundary paradigm with a given detection criterion is
A + D + B + C, with D from the recording. Report median, 95th percentile,
maximum and the number of measurements for each part (≥ 200 boundary crossings
with a normal participant reading for method 1), per frame-rate mode, together
with the monitor model, refresh rate, display mode, graphics card and operating
system.

## Interpretation

For boundary-paradigm reading studies the display change must be complete
during the saccade. Report the proportion of trials in which the change was
complete before saccade offset (from the recorded samples) together with the
latency distribution.

## References

- Saunders, D. R., & Woods, R. L. (2014). Direct measurement of the system
  latency of gaze-contingent displays. *Behavior Research Methods, 46*(2),
  439–447. https://doi.org/10.3758/s13428-013-0375-5
- Anderson, M. D., Cooper, E. A., & Otero-Millan, J. (2026). A method for
  measuring closed-loop latency in gaze-contingent rendering without extra
  equipment. *Behavior Research Methods, 58*, 16.
  https://doi.org/10.3758/s13428-025-02864-3
