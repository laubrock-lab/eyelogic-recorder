"""High-level EyeLogic client with asynchronous recording to CSV.

The EyeLogic SDK delivers gaze samples through a callback on an SDK thread.
``EyeLogicClient`` keeps that callback cheap: it only copies the sample into a
preallocated in-memory chunk. Full chunks are handed to a background writer
thread through a bounded queue, so disk I/O never blocks the SDK callback.
See ``docs/recording_pipeline.md`` for the design.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import warnings
from ctypes import byref, memmove, sizeof
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np

from . import _sdk
from .exceptions import (
    CalibrationError,
    ConnectError,
    ConnectionClosedError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    EyeLogicWarning,
    MessageWriteError,
    NoEyeDetectedError,
    RecordingOverflowError,
    StartTrackingError,
    TrackingStoppedError,
    ValidationError,
    WriterError,
)
from .io import CSV_HEADER, GAZE_FIELDS, MESSAGE_HEADER, SAMPLE_DTYPE, format_rows


@dataclass
class ValidationPoint:
    """Validation result for one target; deviations are ``None`` if the eye was not tracked."""

    x_px: float
    y_px: float
    left_deg: Optional[float]
    left_px: Optional[float]
    right_deg: Optional[float]
    right_px: Optional[float]


class EyeLogicClient:
    """High-level EyeLogic client.

    Typical lifecycle::

        with EyeLogicClient("my_experiment") as et:
            et.connect()
            et.find_tracker()
            et.start_tracking()
            et.calibrate()
            et.validate()
            for trial in trials:
                et.start_recording()
                et.send_message(f"TRIALID {trial.id}")
                ...
                et.send_message("TRIAL_END")
                et.stop_recording()

    Leaving the ``with`` block (or calling :meth:`close`) stops recording and
    tracking, disconnects, and closes the data files.

    Errors are raised as subclasses of :class:`eyelogic_recorder.EyeLogicError`. Failures
    that happen on SDK or writer threads (device unplugged, disk full) are
    raised on the next call to :meth:`check_health` or any method that calls it.
    """

    def __init__(
        self,
        experiment_name: str = "experiment",
        data_folder: str = "data",
        buffer_size: int = 1000,
        writer_queue_size: int = 8,
    ):
        """
        Args:
            experiment_name: Prefix of the data files; also shown in the EyeLogic server window.
            data_folder: Output directory, created if missing.
            buffer_size: Samples per in-memory chunk handed to the writer thread.
            writer_queue_size: Number of full chunks that may wait for the writer. The pool
                holds ``writer_queue_size + 2`` chunks; if the writer falls further behind,
                samples are dropped (``samples_dropped``) rather than delaying the SDK callback.
        """
        self._closed = True  # until fully constructed, close() is a no-op
        self._sdk = _sdk.load()
        self.experiment_name = experiment_name
        self.data_folder = data_folder
        self.buffer_size = buffer_size
        self.writer_queue_size = writer_queue_size

        self._data_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._is_recording = False
        self._connected = False
        self._tracking = False

        self._writer_exception: Optional[Exception] = None
        self._callback_exception: Optional[Exception] = None
        self._message_error: Optional[MessageWriteError] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._write_queue: Optional[queue.Queue] = None
        self._free_chunks: Optional[queue.LifoQueue] = None
        self._current_chunk: Optional[np.ndarray] = None
        self._current_chunk_count = 0
        self._writer_stop_token = object()

        self._frame_rate_mode = None
        self._last_eye_time = None  # time.monotonic() of the last sample with an eye found
        self._rec_samples = 0       # samples / samples with an eye in the current recording
        self._rec_eye_samples = 0

        self.samples_recorded = 0  # accepted into a buffer by the callback
        self.samples_written = 0   # written to the CSV file by the writer thread
        self.samples_dropped = 0   # lost because no buffer was free (see RecordingOverflowError)
        self.max_queue_depth = 0

        self._last_sample = None
        self._api = self._sdk.ELApi(experiment_name)
        self._sample_file, self._log_file = self._open_files()
        self._closed = False

        self._event_callback = self._sdk.DeviceEventCallback(self._device_event_handler)
        self._sdk.register_device_event_callback(self._api, self._event_callback)
        self._sample_callback = self._sdk.GazeSampleCallback(self._gaze_sample_callback)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:  # don't mask the exception that is already propagating
            try:
                self.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Accessors

    @property
    def api(self):
        """The wrapped low-level ``ELApi`` instance (for SDK features not wrapped here)."""
        return self._api

    @property
    def sample_file(self):
        return self._sample_file

    @property
    def log_file(self):
        return self._log_file

    @property
    def last_sample(self):
        """Copy of the most recent ``ELGazeSample``, or ``None`` before the first one.

        Validity is per vector: each eye's point of regard, eye position and pupil,
        and the binocular points of regard, can be invalid independently (equal to the
        SDK's invalid value); the x coordinate tells for each vector. The binocular
        point of regard is valid if at least one eye's is; points of regard need a
        calibration, eye positions only a found eye.
        """
        return self._last_sample

    def get_position(self, as_array: bool = False):
        """Return the latest filtered gaze position, or ``None`` if the last sample was invalid.

        Available whenever tracking runs, also between recordings (e.g. for fixation checks).

        Args:
            as_array: Return ``np.ndarray`` (float32) instead of an ``(x, y)`` tuple.
        """
        self.check_health()
        sample = self._last_sample
        if sample is None or sample.porFilteredX == self._sdk.ELInvalidValue:
            return None
        if as_array:
            return np.array((sample.porFilteredX, sample.porFilteredY), dtype=np.float32)
        return (sample.porFilteredX, sample.porFilteredY)

    def get_active_screen(self) -> Tuple[int, int]:
        """Return the active screen resolution as ``(width, height)``."""
        screen = self._api.getActiveScreen()
        self._log("INFO", "Screen resolution: {}x{}".format(screen.resolutionX, screen.resolutionY))
        return screen.resolutionX, screen.resolutionY

    def get_calibration_methods(self):
        """Return the calibration methods supported by the device (point counts)."""
        return self._api.getDeviceConfig().calibrationMethods

    # ------------------------------------------------------------------
    # Connection, tracking, calibration

    def connect(self):
        """Connect to the local EyeLogic server."""
        self.check_health()
        result = self._api.connect()
        if result != self._sdk.ELApi.ReturnConnect.SUCCESS:
            self._fail(ConnectError("Connection to server failed: {}".format(result)))
        self._connected = True
        self._log("INFO", "Connected to server")

    def disconnect(self):
        """Disconnect from the EyeLogic server."""
        if not self._connected:
            return
        self._connected = False
        self._api.disconnect()
        self._log("INFO", "Disconnected from server")

    def find_tracker(self):
        """Raise :class:`DeviceNotFoundError` unless a tracker is connected to the server."""
        self.check_health()
        config = self._api.getDeviceConfig()
        if config.deviceSerial == 0:
            self._fail(DeviceNotFoundError("No eye tracker device connected"))
        self._log("INFO", "Device found, frame rates: {}".format(list(config.frameRates)))

    def start_tracking(self, frame_rate_mode: int = 0):
        """Start tracking.

        Args:
            frame_rate_mode: Index into the device's frame rates (see ``api.getDeviceConfig()``).
        """
        self.check_health()
        result = self._api.requestTracking(frame_rate_mode)
        if result != self._sdk.ELApi.ReturnStart.SUCCESS:
            self._fail(StartTrackingError("Start tracking failed: {}".format(result)))
        self._tracking = True
        self._frame_rate_mode = frame_rate_mode
        self._api.registerGazeSampleCallback(self._sample_callback)
        self._log("INFO", "Tracking started (frame rate mode {})".format(frame_rate_mode))

    def stop_tracking(self):
        """Stop tracking (stops recording first)."""
        if not self._tracking:
            return
        self.stop_recording()
        self._tracking = False
        self._api.unrequestTracking()
        self._log("INFO", "Tracking stopped")

    def calibrate(self, method: int = 0, busy_timeout: float = 5.0, eye_timeout: Optional[float] = 5.0):
        """Run calibration.

        Args:
            method: Index into :meth:`get_calibration_methods`.
            busy_timeout: Seconds to keep retrying while the server reports ``ALREADY_BUSY``.
            eye_timeout: Seconds to wait for a sample with a detected eye before calibrating;
                raises :class:`NoEyeDetectedError` if none arrives. ``None`` skips the check.
        """
        self.check_health()
        if eye_timeout is not None and not self._wait_for_eye(eye_timeout):
            self._fail(NoEyeDetectedError(
                "No eye detected within {} s (frame rate mode {}). Check that the participant is in "
                "the tracker's view; some devices offer frame rates they do not support "
                "(e.g. 250 Hz on an EyeLogic Lite).".format(eye_timeout, self._frame_rate_mode)))
        result = self._retry_while_busy(lambda: self._api.calibrate(method), busy_timeout, "Calibration")
        if result != self._sdk.ELApi.ReturnCalibrate.SUCCESS:
            self._fail(CalibrationError("Calibration failed: {}".format(result)))
        self._log("INFO", "Calibrated (method {})".format(method))

    def validate(self, busy_timeout: float = 5.0) -> List[ValidationPoint]:
        """Run validation, log per-point deviations, and return them.

        Args:
            busy_timeout: Seconds to keep retrying while the server reports ``ALREADY_BUSY``.
                The server is often still busy for a moment after :meth:`calibrate` returns.
        """
        self.check_health()
        result, data = self._retry_while_busy(self._api.validate, busy_timeout, "Validation")
        if result != self._sdk.ELApi.ReturnValidate.SUCCESS:
            self._fail(ValidationError("Validation failed: {}".format(result)))

        invalid = self._sdk.ELInvalidValue

        def valid_or_none(value):
            return None if value == invalid else value

        points = []
        for p in data.pointsData:
            point = ValidationPoint(
                x_px=p.validationPointPxX,
                y_px=p.validationPointPxY,
                left_deg=valid_or_none(p.meanDeviationLeftDeg),
                left_px=valid_or_none(p.meanDeviationLeftPx),
                right_deg=valid_or_none(p.meanDeviationRightDeg),
                right_px=valid_or_none(p.meanDeviationRightPx),
            )
            points.append(point)
        if points and all(p.left_deg is None and p.right_deg is None for p in points):
            self._fail(ValidationError("Validation found no eye at any target; check the participant's "
                                       "position and the frame rate (see NoEyeDetectedError)."))
        for i, point in enumerate(points, start=1):
            self._log("INFO", "Validation point {} at ({:.0f}, {:.0f}) px: left {}, right {}".format(
                i, point.x_px, point.y_px,
                _format_deviation(point.left_deg, point.left_px),
                _format_deviation(point.right_deg, point.right_px),
            ))
        return points

    # ------------------------------------------------------------------
    # Recording

    def start_recording(self):
        """Start writing samples to the CSV file (tracking must be running)."""
        self.check_health()
        with self._shutdown_lock:
            if self._is_recording:
                return
            self._start_writer()
            with self._data_lock:
                self._rec_samples = 0
                self._rec_eye_samples = 0
                self._is_recording = True
        # Registering again is harmless and covers tracking started outside this client.
        self._api.registerGazeSampleCallback(self._sample_callback)
        self._log("INFO", "Start recording")

    def stop_recording(self):
        """Stop recording and wait until all pending samples are written."""
        with self._shutdown_lock:
            if not self._is_recording:
                return
            self._stop_writer()
        self._log("INFO", "Stop recording")
        self._report_eye_share()

    def send_message(self, text: str):
        """Write a timestamped message (e.g. ``TRIALID 3``) to the message file.

        The timestamp uses the same clock as the ``SysTime`` column of the sample file.
        """
        self._log("MSG", text)

    def flush(self, timeout: float = 10.0):
        """Write everything recorded so far, and all messages, to disk; returns when done.

        The files are synced with ``os.fsync``, so the data are on the disk, not only in
        the operating system's cache.

        Raises :class:`WriterError` if the writer fails or does not finish within ``timeout`` seconds.
        """
        done = None
        with self._data_lock:
            if self._writer_thread is not None and self._write_queue is not None:
                if self._current_chunk is not None and self._current_chunk_count > 0:
                    self._enqueue((self._current_chunk, self._current_chunk_count))
                    self._current_chunk = self._take_free_chunk()
                    self._current_chunk_count = 0
                done = threading.Event()
                self._enqueue(done)  # the writer sets it after everything queued before it
        if done is not None:
            deadline = time.monotonic() + timeout
            while not done.wait(0.05):
                self._raise_if_writer_failed()
                if time.monotonic() > deadline:
                    raise WriterError("flush() timed out after {} s".format(timeout))
            self._raise_if_writer_failed()
        else:
            _sync(self._sample_file)
        with self._log_lock:
            _sync(self._log_file)

    def check_health(self):
        """Raise errors from the SDK callback, the writer thread, or message writing."""
        self._raise_if_writer_failed()
        if self._callback_exception is not None:
            raise self._callback_exception
        if self.samples_dropped:
            raise RecordingOverflowError(
                "{} samples dropped: no free recording buffer (the writer fell behind; "
                "{} samples recorded)".format(self.samples_dropped, self.samples_recorded))
        if self._message_error is not None:
            raise self._message_error

    def close(self):
        """Stop recording and tracking, disconnect, and close files. Safe to call repeatedly.

        Raises :class:`WriterError` after cleanup if pending samples could not be written.
        """
        if self._closed:
            return
        self._closed = True
        writer_error = None
        for step in (self.stop_recording, self.stop_tracking, self.disconnect):
            try:
                step()
            except WriterError as err:
                writer_error = writer_error or err
            except Exception:
                pass
        try:
            self._api.registerGazeSampleCallback(None)
            self._sdk.register_device_event_callback(self._api, None)
        except Exception:
            pass
        self._log("INFO", "Closed")
        for f in (self._sample_file, self._log_file):
            try:
                _sync(f)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass
        if writer_error is not None:
            raise writer_error

    # ------------------------------------------------------------------
    # SDK callbacks (run on SDK threads)

    def _gaze_sample_callback(self, gaze_sample_ptr):
        """Producer side: copy the sample into the current chunk. No disk I/O here."""
        sample = self._sdk.ELGazeSample()
        memmove(byref(sample), gaze_sample_ptr, sizeof(sample))
        self._last_sample = sample
        invalid = self._sdk.ELInvalidValue
        eye = sample.eyePositionLeftX != invalid or sample.eyePositionRightX != invalid
        if eye:
            self._last_eye_time = time.monotonic()

        # Everything below runs under _data_lock and never waits: the EyeLogic server
        # drops samples when the callback returns late.
        with self._data_lock:
            if not self._is_recording or self._writer_exception is not None:
                return
            if self._current_chunk is None:  # pool was empty: try again, else drop
                self._current_chunk = self._take_free_chunk()
                if self._current_chunk is None:
                    self.samples_dropped += 1
                    return
            self._current_chunk[self._current_chunk_count] = (
                sample.timestampMicroSec,
                sample.index,
                sample.porRawX,
                sample.porRawY,
                sample.porFilteredX,
                sample.porFilteredY,
                sample.porLeftX,
                sample.porLeftY,
                sample.eyePositionLeftX,
                sample.eyePositionLeftY,
                sample.eyePositionLeftZ,
                sample.pupilRadiusLeft,
                sample.porRightX,
                sample.porRightY,
                sample.eyePositionRightX,
                sample.eyePositionRightY,
                sample.eyePositionRightZ,
                sample.pupilRadiusRight,
                time.time(),
            )
            self._current_chunk_count += 1
            self.samples_recorded += 1
            self._rec_samples += 1
            self._rec_eye_samples += eye
            if self._current_chunk_count == self.buffer_size:
                # Enqueued under the lock, so it always precedes a later stop token.
                self._enqueue((self._current_chunk, self.buffer_size))
                self._current_chunk = self._take_free_chunk()
                self._current_chunk_count = 0

    def _device_event_handler(self, event: int):
        name = self._sdk.DeviceEvent(event).name
        self._log("EVENT", name)
        # Events caused by our own disconnect()/stop_tracking() are expected, not errors.
        if name == "CONNECTION_CLOSED" and self._connected:
            self._handle_terminal_event(ConnectionClosedError("Connection to EyeLogic server closed"))
        elif name == "DEVICE_DISCONNECTED":
            self._handle_terminal_event(DeviceDisconnectedError("Eye tracker device disconnected"))
        elif name == "TRACKING_STOPPED" and self._tracking:
            self._handle_terminal_event(TrackingStoppedError("Eye tracker stopped tracking"))

    def _handle_terminal_event(self, err: Exception):
        with self._shutdown_lock:
            if self._callback_exception is None:
                self._callback_exception = err
            if self._is_recording:
                try:
                    self._stop_writer()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Writer thread (consumer side) and chunk pool

    def _start_writer(self):
        self._writer_exception = None
        # The pool of preallocated chunks is the only capacity limit; the queue itself is
        # unbounded, so handing over a chunk (or the stop token) never blocks or fails.
        self._write_queue = queue.Queue()
        self._free_chunks = queue.LifoQueue()
        for _ in range(self.writer_queue_size + 2):
            self._free_chunks.put(np.empty(self.buffer_size, dtype=SAMPLE_DTYPE))
        with self._data_lock:
            self._current_chunk = self._free_chunks.get_nowait()
            self._current_chunk_count = 0
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def _writer_loop(self):
        invalid = self._sdk.ELInvalidValue
        try:
            while True:
                item = self._write_queue.get()
                if item is self._writer_stop_token:
                    break
                if isinstance(item, threading.Event):  # flush() marker
                    _sync(self._sample_file)
                    item.set()
                    continue
                chunk, count = item
                if count > 0:
                    rows = chunk[:count]
                    for field in GAZE_FIELDS:
                        column = rows[field]
                        column[column == invalid] = np.nan
                    self._sample_file.write(format_rows(rows))
                    self.samples_written += count
                self._free_chunks.put(chunk)
        except Exception as err:
            self._writer_exception = err

    def _raise_if_writer_failed(self):
        if self._writer_exception is not None:
            raise WriterError("Writer thread failed: {}".format(self._writer_exception)) from self._writer_exception

    def _take_free_chunk(self):
        """A free chunk from the pool, or None if all are in use. Never waits."""
        try:
            return self._free_chunks.get_nowait()
        except queue.Empty:
            return None

    def _enqueue(self, item):
        """Hand an item to the writer. Caller holds ``_data_lock``; never waits."""
        self._write_queue.put_nowait(item)
        self.max_queue_depth = max(self.max_queue_depth, self._write_queue.qsize())

    def _stop_writer(self):
        """Queue the partial chunk and the stop token, join the writer. Caller holds ``_shutdown_lock``."""
        if self._writer_thread is None:
            return
        with self._data_lock:
            # Same lock as the callback: no sample is accepted after this point, and every
            # chunk the callback filled is already queued ahead of the stop token.
            self._is_recording = False
            if self._current_chunk is not None and self._current_chunk_count > 0:
                self._enqueue((self._current_chunk, self._current_chunk_count))
            self._current_chunk = None
            self._current_chunk_count = 0
            self._enqueue(self._writer_stop_token)
        self._writer_thread.join()
        self._writer_thread = None
        self._write_queue = None
        self._free_chunks = None
        self._raise_if_writer_failed()
        _sync(self._sample_file)  # each finished recording is on the disk

    # ------------------------------------------------------------------
    # Files

    def _open_files(self):
        os.makedirs(self.data_folder, exist_ok=True)
        stem = "{}_{:%Y%m%d_%H%M%S}".format(self.experiment_name, datetime.now())
        base = os.path.join(self.data_folder, stem)
        n = 1
        while True:
            try:
                sample_file = open(base + ".csv", "x", newline="", encoding="utf-8")
                break
            except FileExistsError:
                n += 1
                base = os.path.join(self.data_folder, "{}_{}".format(stem, n))
        sample_file.write(",".join(CSV_HEADER) + "\n")
        log_file = open(base + ".tsv", "x", newline="", encoding="utf-8")
        log_file.write("\t".join(MESSAGE_HEADER) + "\n")
        return sample_file, log_file

    def _retry_while_busy(self, call, timeout: float, what: str):
        """Call ``call()`` until its result is not ``ALREADY_BUSY`` or ``timeout`` seconds passed.

        The EyeLogic server is often still busy for a moment after calibrate() returns
        (a known server bug, confirmed by EyeLogic), so an immediate validate() would fail
        without this.
        """
        deadline = time.monotonic() + timeout
        logged = False
        while True:
            out = call()
            result = out[0] if isinstance(out, tuple) else out
            if getattr(result, "name", None) != "ALREADY_BUSY" or time.monotonic() >= deadline:
                return out
            if not logged:
                self._log("INFO", "{}: EyeLogic server busy, waiting".format(what))
                logged = True
            time.sleep(0.1)

    def _wait_for_eye(self, timeout: float) -> bool:
        """True as soon as a sample with a detected eye arrives (or arrived in the last 0.5 s)."""
        deadline = time.monotonic() + timeout
        while True:
            last = self._last_eye_time
            if last is not None and time.monotonic() - last < 0.5:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _report_eye_share(self):
        """Log the share of samples with a detected eye; warn loudly if there were none."""
        n, eyes = self._rec_samples, self._rec_eye_samples
        if n == 0:
            msg = "Recording contained no samples (is tracking running?)"
        else:
            self._log("INFO", "Recording: {} samples, eye found in {:.0%}".format(n, eyes / n))
            if eyes:
                return
            msg = ("Recording contained no eye in any of its {} samples (participant not in view, or "
                   "a frame rate the device does not support, e.g. 250 Hz on an EyeLogic Lite)".format(n))
        self._log("WARNING", msg)
        warnings.warn(EyeLogicWarning(msg), stacklevel=3)

    def _fail(self, err: Exception):
        """Write the error to the message file, then raise it."""
        self._log("ERROR", str(err))
        raise err

    def _log(self, kind: str, text: str):
        text = str(text).replace("\t", " ").replace("\r", " ").replace("\n", " ")
        with self._log_lock:
            try:
                self._log_file.write("{:.6f}\t{}\t{}\n".format(time.time(), kind, text))
            except Exception as err:
                # Also called from SDK threads, so the error is kept for check_health().
                if self._message_error is None:
                    self._message_error = MessageWriteError(
                        "Could not write {} message {!r}: {}".format(kind, text, err))


def _sync(f):
    """Flush Python's buffer and have the OS write the file to the disk."""
    f.flush()
    os.fsync(f.fileno())


def _format_deviation(deg, px):
    if deg is None:
        return "N/A"
    return "{:.2f} deg ({:.1f} px)".format(deg, px)
