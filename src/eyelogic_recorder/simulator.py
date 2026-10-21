"""Simulated EyeLogic SDK, for developing and testing without a tracker.

``install()`` puts a fake ``eyelogic.ELApi`` module into ``sys.modules``; an
``EyeLogicClient`` created afterwards talks to the simulator instead of the
real SDK. Samples are delivered through ctypes callbacks from a background
thread at the device frame rate, like the real SDK::

    from eyelogic_recorder import simulator, EyeLogicClient
    simulator.install()                          # synthetic fixations, saccades, blinks
    simulator.install(position=lambda: (x, y))   # e.g. follow the mouse

The ``ELGazeSample`` layout matches the real SDK. Timestamps are host-clock
microseconds since the epoch (an assumption about the real device, see README).
"""

from __future__ import annotations

import random
import sys
import threading
import time
import types
from ctypes import CFUNCTYPE, POINTER, Structure, c_double, c_int32, c_int64, pointer
from enum import Enum
from typing import Callable, Optional, Tuple

INVALID = -1.0e300  # stands in for the DLL's ELCInvalidValue
FRAME_RATES = [60, 120, 250]
CALIBRATION_METHODS = [1, 5, 9]


class ELGazeSample(Structure):
    _fields_ = [
        ("timestampMicroSec", c_int64),
        ("index", c_int32),
        ("porRawX", c_double),
        ("porRawY", c_double),
        ("porFilteredX", c_double),
        ("porFilteredY", c_double),
        ("porLeftX", c_double),
        ("porLeftY", c_double),
        ("eyePositionLeftX", c_double),
        ("eyePositionLeftY", c_double),
        ("eyePositionLeftZ", c_double),
        ("pupilRadiusLeft", c_double),
        ("porRightX", c_double),
        ("porRightY", c_double),
        ("eyePositionRightX", c_double),
        ("eyePositionRightY", c_double),
        ("eyePositionRightZ", c_double),
        ("pupilRadiusRight", c_double),
    ]


class DeviceEvent(Enum):
    SCREEN_CHANGED = 0
    CONNECTION_CLOSED = 1
    DEVICE_CONNECTED = 2
    DEVICE_DISCONNECTED = 3
    TRACKING_STOPPED = 4


GazeSampleCallback = CFUNCTYPE(None, POINTER(ELGazeSample))
DeviceEventCallback = CFUNCTYPE(None, c_int32)


def make_sample(index, x=None, y=None, timestamp_us=None, valid=True):
    """Build an ``ELGazeSample``; by default every gaze field equals ``index``."""
    s = ELGazeSample()
    s.timestampMicroSec = timestamp_us if timestamp_us is not None else 1_700_000_000_000_000 + index * 4000
    s.index = index
    if not valid:
        for name, _ in ELGazeSample._fields_[2:]:
            setattr(s, name, INVALID)
        return s
    if x is None:
        for name, _ in ELGazeSample._fields_[2:]:
            setattr(s, name, float(index))
        return s
    for prefix in ("porRaw", "porFiltered", "porLeft", "porRight"):
        setattr(s, prefix + "X", x)
        setattr(s, prefix + "Y", y)
    s.eyePositionLeftX, s.eyePositionLeftY, s.eyePositionLeftZ = -30.0, 0.0, 600.0
    s.eyePositionRightX, s.eyePositionRightY, s.eyePositionRightZ = 30.0, 0.0, 600.0
    s.pupilRadiusLeft = s.pupilRadiusRight = 2.0
    return s


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ScanpathGenerator:
    """Synthetic gaze: fixations of 150-400 ms at random screen positions, a ~150 ms blink every few seconds."""

    def __init__(self, screen=(1920, 1080), seed=1):
        self._rng = random.Random(seed)
        self._screen = screen
        self._pos = (screen[0] / 2, screen[1] / 2)
        self._fix_end = 0.0
        self._blink_start = 3.0
        self._blink_end = 3.15

    def __call__(self, t) -> Optional[Tuple[float, float]]:
        if t >= self._blink_end:
            self._blink_start = t + self._rng.uniform(2.0, 5.0)
            self._blink_end = self._blink_start + self._rng.uniform(0.1, 0.2)
        if self._blink_start <= t < self._blink_end:
            return None
        if t >= self._fix_end:
            w, h = self._screen
            self._pos = (self._rng.uniform(0.1, 0.9) * w, self._rng.uniform(0.1, 0.9) * h)
            self._fix_end = t + self._rng.uniform(0.15, 0.4)
        return self._pos


class SimulatedELApi:
    """Fake ``ELApi``. Configure via :func:`install`; the instance is ``client.api``."""

    rate_hz: Optional[float] = None  # None: use FRAME_RATES[mode]; 0: no thread, call emit() yourself
    position: Optional[Callable[[], Optional[Tuple[float, float]]]] = None
    screen = (1920, 1080)
    record_timing = False
    busy_after_calibration_s = 0.0  # like the real server: still busy for a while after calibrate() returns

    class ReturnConnect(Enum):
        SUCCESS = 0
        NOT_INITED = 1
        VERSION_MISMATCH = 2
        TIMEOUT = 3

    class ReturnStart(Enum):
        SUCCESS = 0
        NOT_CONNECTED = 1
        DEVICE_MISSING = 2
        INVALID_FRAMERATE_MODE = 3
        ALREADY_RUNNING_DIFFERENT_FRAMERATE = 4
        FAILURE = 5

    class ReturnCalibrate(Enum):
        SUCCESS = 0
        NOT_CONNECTED = 1
        NOT_TRACKING = 2
        INVALID_CALIBRATION_MODE = 3
        ALREADY_BUSY = 4
        FAILURE = 5

    class ReturnValidate(Enum):
        SUCCESS = 0
        NOT_CONNECTED = 1
        NOT_TRACKING = 2
        NOT_CALIBRATED = 3
        ALREADY_BUSY = 4
        FAILURE = 5

    def __init__(self, client_name):
        self.client_name = client_name
        self.sample_cb = None
        self.event_cb = None
        self.connect_result = SimulatedELApi.ReturnConnect.SUCCESS
        self.calls = []
        self.emitted = 0
        self.timing = []  # (nominal_s, call_start_s, call_end_s) per sample if record_timing
        self._thread = None
        self._stop = threading.Event()
        self._busy_until = 0.0

    # --- SDK interface ---------------------------------------------------

    def registerGazeSampleCallback(self, cb):
        self.sample_cb = cb

    def _register_device_event_callback(self, cb):
        self.event_cb = cb

    def connect(self):
        self.calls.append("connect")
        return self.connect_result

    def disconnect(self):
        self.calls.append("disconnect")
        self._stop_thread()
        self.fire(DeviceEvent.CONNECTION_CLOSED)

    def isConnected(self):
        return "connect" in self.calls and "disconnect" not in self.calls

    def requestTracking(self, mode):
        self.calls.append(("requestTracking", mode))
        if not 0 <= mode < len(FRAME_RATES):
            return SimulatedELApi.ReturnStart.INVALID_FRAMERATE_MODE
        rate = FRAME_RATES[mode] if self.rate_hz is None else self.rate_hz
        if rate:
            self._start_thread(rate)
        return SimulatedELApi.ReturnStart.SUCCESS

    def unrequestTracking(self):
        self.calls.append("unrequestTracking")
        self._stop_thread()
        self.fire(DeviceEvent.TRACKING_STOPPED)

    def _busy(self):
        return time.monotonic() < self._busy_until

    def calibrate(self, mode):
        self.calls.append(("calibrate", mode))
        if self._busy():
            return SimulatedELApi.ReturnCalibrate.ALREADY_BUSY
        self._busy_until = time.monotonic() + self.busy_after_calibration_s
        return SimulatedELApi.ReturnCalibrate.SUCCESS

    def validate(self):
        self.calls.append("validate")
        if self._busy():
            return SimulatedELApi.ReturnValidate.ALREADY_BUSY, _Obj(pointsData=[])
        w, h = self.screen
        points = [
            _Obj(validationPointPxX=px, validationPointPxY=py,
                 meanDeviationLeftPx=10.0, meanDeviationLeftDeg=0.3,
                 meanDeviationRightPx=INVALID, meanDeviationRightDeg=INVALID)
            for px, py in ((w / 4, h / 4), (3 * w / 4, h / 4), (w / 4, 3 * h / 4), (3 * w / 4, 3 * h / 4))
        ]
        return SimulatedELApi.ReturnValidate.SUCCESS, _Obj(pointsData=points)

    def getDeviceConfig(self):
        return _Obj(deviceSerial=42, frameRates=list(FRAME_RATES), calibrationMethods=list(CALIBRATION_METHODS))

    def getActiveScreen(self):
        return _Obj(resolutionX=self.screen[0], resolutionY=self.screen[1])

    # --- simulation --------------------------------------------------------

    def emit(self, sample):
        """Deliver one sample through the registered callback, as the SDK would."""
        cb = self.sample_cb
        if cb:
            cb(pointer(sample))

    def fire(self, event):
        cb = self.event_cb
        if cb:
            cb(event.value)

    def _start_thread(self, rate):
        self._stop_thread()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(rate,), daemon=True)
        self._thread.start()

    def _stop_thread(self):
        if self._thread is not None:
            self._stop.set()
            if self._thread is not threading.current_thread():
                self._thread.join()
            self._thread = None

    def _run(self, rate):
        period = 1.0 / rate
        source = self.position or ScanpathGenerator(self.screen)
        t0 = time.time()
        i = 0
        while not self._stop.is_set():
            nominal = t0 + i * period
            delay = nominal - time.time()
            if delay > 0.004:
                time.sleep(delay - 0.003)  # OS sleeps can overshoot by a few ms (timer coalescing)
            while time.time() < nominal:  # spin the remaining milliseconds for accurate pacing,
                time.sleep(0)  # releasing the GIL: the real SDK thread is native and never holds it
            pos = source() if self.position else source(nominal - t0)
            if pos is None:
                sample = make_sample(i, timestamp_us=int(nominal * 1e6), valid=False)
            else:
                sample = make_sample(i, x=float(pos[0]), y=float(pos[1]), timestamp_us=int(nominal * 1e6))
            call_time = time.time()
            start = time.perf_counter()
            self.emit(sample)
            if self.record_timing:
                self.timing.append((nominal, call_time, time.perf_counter() - start))
            self.emitted += 1
            i += 1


def make_modules(rate_hz=None, position=None, screen=(1920, 1080), record_timing=False):
    """Build fake ``eyelogic`` and ``eyelogic.ELApi`` modules (without installing them)."""
    mod = types.ModuleType("eyelogic.ELApi")
    mod.ELGazeSample = ELGazeSample
    mod.ELInvalidValue = INVALID
    mod.GazeSampleCallback = GazeSampleCallback

    api = type("ELApi", (SimulatedELApi,), {
        "rate_hz": rate_hz,
        "position": staticmethod(position) if position else None,
        "screen": screen,
        "record_timing": record_timing,
    })
    mod.ELDeviceEvent = DeviceEvent
    mod.DeviceEventCallback = DeviceEventCallback
    api.registerDeviceEventCallback = SimulatedELApi._register_device_event_callback
    mod.ELApi = api

    pkg = types.ModuleType("eyelogic")
    pkg.ELApi = mod
    return pkg, mod


def install(**kwargs):
    """Replace the EyeLogic SDK with the simulator for this process. Arguments as :func:`make_modules`.

    Args:
        rate_hz: Sample rate; ``None`` uses the requested frame-rate mode, 0 disables the thread.
        position: Callable returning ``(x, y)`` in screen pixels or ``None`` (blink); default synthetic scanpath.
        screen: Simulated screen resolution.
        record_timing: Store per-sample delivery timing in ``api.timing`` (for benchmarks).
    """
    pkg, mod = make_modules(**kwargs)
    sys.modules["eyelogic"] = pkg
    sys.modules["eyelogic.ELApi"] = mod
    return mod
