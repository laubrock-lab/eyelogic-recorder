# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-21

First public release. eyelogic-recorder is the standalone successor of the
in-lab module `jl_eyelogic` (0.1.0): a package with `src` layout
(`pip install eyelogic-recorder`, `import eyelogic_recorder`). The recording
code was reviewed by EyeLogic and tested on EyeLogic hardware.

### Migrating from `jl_eyelogic`

This release is a clean API break. The class `EyeLogicClient` replaces the old
`jl_eyelogic` class, and methods are snake_case.

| Old | New |
|---|---|
| `jl_eyelogic` | `EyeLogicClient` |
| `getPos()`, `getPosNP()` | `get_position(as_array=False)` |
| `getLastSample()` | `last_sample` property |
| `getLogFile()`, `getSampleFile()`, `getAPI()` | `log_file`, `sample_file`, `api` properties |
| `getActiveScreen()` | `get_active_screen()` |
| `getCalibMethods()` | `get_calibration_methods()` |
| `calibrate(calibMethod)` | `calibrate(method)` |
| `start_tracking()` | `start_tracking(frame_rate_mode=0)` |
| `openFiles()`, `closeFiles()` | private (files are opened in the constructor and closed by `close()`) |
| `registerSampleCallback`, `registerEventCallback`, `registerEyeImageCallback`, `start_recording_with_callback` | removed; use `client.api` for raw SDK access |
| `queue_block_events` | `samples_dropped` (the sample callback no longer waits, see "Changed") |
| `JLEyeLogicError` | `EyeLogicError` |

### Added

- `send_message()` for timestamped messages in the TSV file, on the same clock
  as the samples.
- `start_tracking(frame_rate_mode)` selects the frame rate; `stop_tracking()`.
- `validate()` returns a list of `ValidationPoint`.
- `load_samples()` and `load_messages()` for reading recorded files.
- Checks for missing eye data: `calibrate()` waits up to `eye_timeout` seconds
  (default 5) for a detected eye and otherwise raises `NoEyeDetectedError`;
  `validate()` raises `ValidationError` if no eye was found at any target; after
  each recording the share of samples with a detected eye is logged, with a
  `WARNING` line and an `EyeLogicWarning` if it is 0 or no samples arrived. A
  device can offer a frame rate it does not support: an EyeLogic Lite offered
  250 Hz, which only the LogicOne supports, and found no eye in that mode.
- `calibrate()` and `validate()` retry for up to `busy_timeout` seconds
  (default 5) while the server reports `ALREADY_BUSY`. Right after a calibration
  the EyeLogic server is briefly busy (a known server bug); in hardware tests,
  `validate()` called right after `calibrate()` failed this way in 16 of 20 runs.
- Failures (connect, device, tracking, calibration, validation) are written to
  the message file as `ERROR` lines before the exception is raised.
- New exceptions `RecordingOverflowError`, `MessageWriteError`,
  `NoEyeDetectedError` and `WriterError` (raised when the background writer
  thread fails); warning class `EyeLogicWarning`.
- Counters `samples_recorded` and `samples_dropped`, besides `samples_written`
  and `max_queue_depth`.
- `eyelogic_recorder.simulator`: simulated tracker for development, teaching and
  tests.
- PsychoPy examples with `--simulate`: boundary paradigm, moving window for
  reading or scene viewing, paragraph reading with word areas of interest, free
  viewing.
- `benchmarks/`: synthetic throughput and lag comparison, recording quality on
  hardware, gaze-contingent latency protocol.

### Changed

- The EyeLogic SDK is no longer vendored. The package depends on `eyelogic-sdk`,
  pinned to the tested version (1.2.4); only its device-event API (SDK >= 1.2)
  is supported. The EyeLogic server must be at least as new as the SDK.
- The sample callback never waits (the EyeLogic server drops samples when the
  callback returns late). Capacity is limited only by the pool of preallocated
  buffers (40 s at 250 Hz by default); if none is free, samples are dropped,
  counted in `samples_dropped`, and reported by `check_health()` as
  `RecordingOverflowError`; recording continues.
- `flush()` waits until everything recorded so far is written. `flush()`,
  `stop_recording()` and `close()` sync the files to the disk (`os.fsync`).
- Errors writing the message file are raised by `check_health()` as
  `MessageWriteError` instead of being ignored.
- Invalid samples are recorded (as `NA`) instead of being dropped. Validity is
  per vector (each eye, binocular point of regard, eye position), as documented
  in the README.
- `last_sample` keeps every sample, also when some of its fields are invalid.
  `get_position()` works between recordings and returns `None` when the
  filtered position is invalid.
- `close()` also stops tracking and disconnects, and raises `WriterError` after
  cleanup if pending samples could not be written (unless another exception is
  already propagating out of a `with` block).
- The log file is now a TSV with a header (was `.txt`).
- Filenames use ISO-like timestamps (`<experiment>_<YYYYmmdd_HHMMSS>`) and
  existing files are never overwritten.
- Numeric CSV fields are no longer space-padded.

### Fixed

- Samples could be lost at the end of a recording: a buffer filled by the sample
  callback while `stop_recording()` ran could be queued after the writer's stop
  token. All hand-overs to the writer now happen under one lock.
- Terminal device events (connection closed, device disconnected, tracking
  stopped) racing with `stop_recording()`.
- `validate()` logged Y before X and assumed exactly 4 points.

## [0.1.0]

In-lab versions as `jl_eyelogic` (2024–2026), not released.
