"""Lightweight EyeLogic client with asynchronous recording to CSV that never blocks the SDK callback."""

from .client import EyeLogicClient, ValidationPoint
from .exceptions import (
    CalibrationError,
    ConnectError,
    ConnectionClosedError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    EyeLogicError,
    EyeLogicWarning,
    MessageWriteError,
    NoEyeDetectedError,
    RecordingOverflowError,
    StartTrackingError,
    TrackingStoppedError,
    ValidationError,
    WriterError,
)
from .io import load_messages, load_samples

__all__ = [
    "CalibrationError",
    "ConnectError",
    "ConnectionClosedError",
    "DeviceDisconnectedError",
    "DeviceNotFoundError",
    "EyeLogicClient",
    "EyeLogicError",
    "EyeLogicWarning",
    "MessageWriteError",
    "NoEyeDetectedError",
    "RecordingOverflowError",
    "StartTrackingError",
    "TrackingStoppedError",
    "ValidationError",
    "ValidationPoint",
    "WriterError",
    "load_messages",
    "load_samples",
]

__author__ = "Jochen Laubrock"
__version__ = "0.2.0"
