"""Calibration and validation against a server that is briefly busy.

On real hardware (Windows, EyeLogic server, 2026-10-16), validate() called
directly after calibrate() returned ALREADY_BUSY in 16 of 20 runs: the
server was still finishing the calibration. The client must wait for it
instead of failing, and errors must be written to the message file.
"""

import time

import pytest

from eyelogic_recorder import CalibrationError, ConnectError, ValidationError, load_messages
from eyelogic_recorder.simulator import make_sample


def start(client):
    client.connect()
    client.start_tracking()
    client.api.emit(make_sample(0, x=500.0, y=400.0))  # an eye is found (calibrate() checks this)


def messages(client, kind):
    client.flush()
    return [m.text for m in load_messages(client.log_file.name) if m.type == kind]


def test_validate_waits_while_server_finishes_calibration(client):
    start(client)
    client.api.busy_after_calibration_s = 0.3
    client.calibrate()
    t0 = time.monotonic()
    points = client.validate()
    assert len(points) == 4
    assert 0.25 <= time.monotonic() - t0 < 2.0
    assert any("busy" in text for text in messages(client, "INFO"))


def test_calibrate_waits_while_server_busy(client):
    start(client)
    client.api.busy_after_calibration_s = 0.3
    client.calibrate()
    client.calibrate()  # second call hits the busy server, waits, then succeeds
    assert client.api.calls.count(("calibrate", 0)) >= 3


def test_validate_gives_up_after_busy_timeout_and_logs_error(client):
    start(client)
    client.api.busy_after_calibration_s = 5.0
    client.calibrate()
    t0 = time.monotonic()
    with pytest.raises(ValidationError, match="ALREADY_BUSY"):
        client.validate(busy_timeout=0.3)
    assert time.monotonic() - t0 < 2.0
    errors = messages(client, "ERROR")
    assert errors and "ALREADY_BUSY" in errors[-1]


def test_other_failures_are_not_retried(client):
    start(client)
    client.api.connect_result = client.api.ReturnConnect.TIMEOUT
    t0 = time.monotonic()
    with pytest.raises(ConnectError):
        client.connect()
    assert time.monotonic() - t0 < 0.1
    assert any("Connection to server failed" in text for text in messages(client, "ERROR"))


def test_calibration_error_is_logged(client, monkeypatch):
    start(client)
    monkeypatch.setattr(client.api, "calibrate", lambda mode: client.api.ReturnCalibrate.NOT_TRACKING)
    with pytest.raises(CalibrationError):
        client.calibrate()
    assert any("NOT_TRACKING" in text for text in messages(client, "ERROR"))
