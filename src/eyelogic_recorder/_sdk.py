"""Import of the EyeLogic SDK (``eyelogic-sdk``, pinned in pyproject.toml).

eyelogic-recorder is built against one SDK version and uses its device-event API
(``ELDeviceEvent``, ``DeviceEventCallback``, ``registerDeviceEventCallback``,
SDK >= 1.2). The installed EyeLogic server must be at least as new as the SDK;
newer servers stay compatible with older SDKs.

The SDK is imported lazily so that ``eyelogic_recorder.io`` (reading recorded files) works
on any platform, without the Windows-only SDK installed.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace


def load() -> SimpleNamespace:
    """Import ``eyelogic.ELApi`` and return the names ``eyelogic_recorder`` uses."""
    try:
        sdk = importlib.import_module("eyelogic.ELApi")
    except ImportError as err:
        raise ImportError(
            "The EyeLogic SDK is not installed (`pip install eyelogic-sdk`; "
            "installing eyelogic-recorder on Windows installs the supported version)."
        ) from err
    if not hasattr(sdk, "ELDeviceEvent"):
        raise ImportError("eyelogic-recorder needs eyelogic-sdk >= 1.2 (device-event API); found an older SDK.")
    return SimpleNamespace(
        ELApi=sdk.ELApi,
        ELGazeSample=sdk.ELGazeSample,
        ELInvalidValue=sdk.ELInvalidValue,
        GazeSampleCallback=sdk.GazeSampleCallback,
        DeviceEvent=sdk.ELDeviceEvent,
        DeviceEventCallback=sdk.DeviceEventCallback,
        register_device_event_callback=lambda api, callback: api.registerDeviceEventCallback(callback),
    )
