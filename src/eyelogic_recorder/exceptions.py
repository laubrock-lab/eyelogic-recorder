"""Exception hierarchy for ``eyelogic_recorder``."""


class EyeLogicError(Exception):
    """Base exception for all ``eyelogic_recorder`` failures."""


class ConnectError(EyeLogicError):
    """Raised when connecting to the EyeLogic server fails."""


class DeviceNotFoundError(EyeLogicError):
    """Raised when no eye-tracker device is available."""


class StartTrackingError(EyeLogicError):
    """Raised when tracking cannot be started."""


class CalibrationError(EyeLogicError):
    """Raised when calibration fails."""


class ValidationError(EyeLogicError):
    """Raised when validation fails."""


class ConnectionClosedError(EyeLogicError):
    """Raised when the EyeLogic server connection closes unexpectedly."""


class DeviceDisconnectedError(EyeLogicError):
    """Raised when the eye-tracker device disconnects unexpectedly."""


class TrackingStoppedError(EyeLogicError):
    """Raised when tracking stops unexpectedly while the client is active."""


class WriterError(EyeLogicError):
    """Raised when the background writer thread failed to write samples."""


class RecordingOverflowError(EyeLogicError):
    """Raised when samples were dropped because no recording buffer was free.

    The sample callback never waits (the EyeLogic server drops samples when it
    does), so if the writer falls behind by more than the buffer pool, samples
    are dropped and counted (``samples_dropped``); recording continues.
    """


class MessageWriteError(EyeLogicError):
    """Raised when a message (e.g. a trial marker) could not be written."""


class NoEyeDetectedError(EyeLogicError):
    """Raised when no eye is detected, e.g. before a calibration.

    Causes: the participant is not in the tracker's view, or the device offers a
    frame rate it does not support (e.g. 250 Hz on an EyeLogic Lite).
    """


class EyeLogicWarning(UserWarning):
    """Warning about data quality, e.g. a recording without any detected eye."""
