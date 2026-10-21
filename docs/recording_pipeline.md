# Recording Internals (Developer Notes)

This file documents the threaded recording pipeline in `src/eyelogic_recorder/client.py`
(`EyeLogicClient`). File formats live in `src/eyelogic_recorder/io.py`, the SDK
import in `src/eyelogic_recorder/_sdk.py`.

## Why this exists

The EyeLogic sample callback is a hot path: the EyeLogic server drops
samples when the callback does not return quickly. The callback must
therefore never wait, neither on disk I/O nor on a full buffer. The design
separates concerns:

- Producer: SDK callback thread (fast, no disk I/O)
- Consumer: background writer thread (slow disk I/O)

## Mental model

```text
EyeLogic callback -> current in-memory chunk -> write queue -> writer thread -> CSV file
                                     ^                                   |
                                     |--------- free chunk pool <--------|
```

Chunks are preallocated NumPy structured arrays (`SAMPLE_DTYPE`,
`buffer_size` rows each). The pool holds `writer_queue_size + 2` chunks and is
the only capacity limit: `_write_queue` itself is unbounded, so handing a chunk
(or the stop token, or a flush marker) to the writer never blocks or fails.

## Lifecycle

1. `start_tracking()` requests tracking and registers the gaze callback
   (`_gaze_sample_callback`). The callback is therefore active outside
   recording too, so `last_sample` and `get_position()` work between
   recordings; only recording writes to chunks.

2. `start_recording()` (under `_shutdown_lock`):
   - `_start_writer()` creates the queue and chunk pool, takes the first
     current chunk, and starts the writer thread
   - sets `_is_recording = True` (under `_data_lock`)
   - registers the gaze callback again (harmless; covers tracking started
     outside the client)

3. `_gaze_sample_callback` (SDK thread):
   - copies the SDK sample memory into a Python-owned struct (`memmove`) and
     stores it as `_last_sample` (every sample; validity is per vector)
   - everything else runs under `_data_lock` and never waits: returns early if
     not recording or the writer failed; otherwise appends one row (including
     `time.time()` as `sysTime`) to `_current_chunk` and counts it in
     `samples_recorded`
   - when the chunk is full, hands it to the writer (`_enqueue`, `put_nowait`)
     and takes a fresh one from the pool (`_take_free_chunk`, `get_nowait`).
     Both happen under the lock, so a full chunk is always queued before any
     later stop token.
   - if the pool is empty (the writer fell behind by the whole pool), the
     current chunk stays `None` and samples are dropped and counted in
     `samples_dropped` until a chunk is free again; see Overload policy
   - raw values are stored as-is; no conversion happens here

4. Writer thread (`_writer_loop`):
   - blocks on `_write_queue.get()`, stops on the stop token; for a flush
     marker (`threading.Event`) it flushes the file and sets the event
   - converts the SDK invalid marker to NaN in the gaze fields (off the hot
     path), formats rows with `io.format_rows` (NaN is written as `NA`),
     writes them, and increments `samples_written`
   - returns the chunk to the pool (`_free_chunks`)
   - any exception is stored in `_writer_exception`

5. `stop_recording()` (under `_shutdown_lock`) calls `_stop_writer()`, which,
   under `_data_lock`, clears `_is_recording`, queues the partial chunk and
   then the stop token. Since the callback queues full chunks under the same
   lock, no filled chunk can end up behind the stop token. It then joins the
   writer, drops the queue and pool, re-raises a stored writer error as
   `WriterError`, and flushes the file.

6. `stop_tracking()` stops recording first, clears `_tracking`, then calls
   `unrequestTracking()`. `disconnect()` clears `_connected`, then calls the
   SDK. Flags are cleared before the SDK call so the resulting device events
   (`TRACKING_STOPPED`, `CONNECTION_CLOSED`) are recognised as self-inflicted
   and not treated as errors.

7. `close()` is idempotent: it runs `stop_recording`, `stop_tracking`,
   `disconnect` (each guarded), unregisters both callbacks, logs `Closed`, and
   closes both files. `__exit__` and `__del__` delegate to it.

## Device events

`_device_event_handler` logs every event as `EVENT` in the message file.
For unexpected `CONNECTION_CLOSED` (only while `_connected`),
`DEVICE_DISCONNECTED`, and `TRACKING_STOPPED` (only while `_tracking`) it calls
`_handle_terminal_event`, which stores the exception in `_callback_exception`
and, if recording, stops it gracefully (`_stop_writer`) so already captured
samples are written. The exception reaches application code on the
next `check_health()` (or any method that calls it).

## Locking

- `_data_lock` guards the chunk state (`_current_chunk`,
  `_current_chunk_count`), the `_is_recording` flag, and all hand-overs to the
  writer queue (full chunks, partial chunk at stop or flush, stop token, flush
  marker). The callback holds it only for non-blocking operations. Because the
  flag is cleared and the stop token queued under the same lock, the callback
  can never write into a chunk that is being torn down, and no filled chunk is
  queued after the stop token.
- `_shutdown_lock` serialises `start_recording`, `stop_recording`, and
  `_handle_terminal_event`, so a terminal device event cannot race with a user
  call to `stop_recording`.
- `_log_lock` guards writes to the message file (`_log`), which is called from
  user, SDK, and device-event threads.

## Overload policy

- Memory is bounded by the chunk pool (`writer_queue_size + 2` chunks of
  `buffer_size` samples; by default 10 000 samples, 40 s at 250 Hz).
- The callback never waits. Waiting would not prevent loss anyway: the EyeLogic
  server itself drops samples when the callback returns late, and it would
  hide the loss. So if no chunk is free, samples are dropped, counted in
  `samples_dropped`, and `check_health()` raises `RecordingOverflowError`.
  Recording continues as soon as the writer returns a chunk; the gap is
  visible in the `Index` column.
- Public counters for diagnosing slow disks: `samples_recorded` (accepted by
  the callback), `samples_written` (rows written), `samples_dropped`, and
  `max_queue_depth` (highest observed queue length). After `stop_recording()`
  or `flush()`, `samples_written == samples_recorded`.

## Implementation notes

- No formatting or file writing happens in the callback path.
- `last_sample` must be copied out of callback memory before the callback
  returns.
- `flush()` queues the partial chunk and a flush marker (under `_data_lock`)
  and waits until the writer has processed everything queued before the
  marker and flushed the file (`WriterError` on writer failure or timeout);
  then it flushes the message file. Both files are synced with `os.fsync`, so
  the data are on the disk, not only in the OS cache; the same happens after
  every `stop_recording()` and in `close()`.
- Errors writing the message file (`_log`, called from user and SDK threads)
  are stored and raised by `check_health()` as `MessageWriteError`, so lost
  trial markers do not pass silently.
- `_sdk.load()` imports the SDK lazily (so `eyelogic_recorder.io` works without it) and
  requires the device-event API of SDK >= 1.2; the dependency is pinned to the
  tested version. The EyeLogic server must be at least as new as the SDK.
- `ETTime` (SDK `timestampMicroSec`) is on the host clock, so
  `SysTime − ETTime` approximates the delivery latency from acquisition to the callback. In a
  hardware test (2026-10-20) it was negative for 5–15% of samples (down to
  −13 ms) and the offset shifted within runs, so use medians, not single
  samples.
  Messages and samples are aligned via `SysTime`, which is unaffected.
- Output files are opened with mode `"x"`, so existing files are never
  overwritten; a numeric suffix is appended on collision.

## Known caveats

- Asynchronous errors are only raised when the application calls
  `check_health()` (or a method that does). Call it regularly.
- Runtime behaviour should be validated on real hardware, especially for long
  recordings at high sample rates.
