"""Loud failures when no eye is tracked.

Hardware test (2026-10-16): an EyeLogic Lite offers 250 Hz but tracks no eye in
that mode; the recording ran without any warning and contained no eye data.
"""

import warnings

import pytest

from eyelogic_recorder import EyeLogicWarning, NoEyeDetectedError, ValidationError, load_messages
from eyelogic_recorder.simulator import make_sample


def start(client):
    client.connect()
    client.start_tracking()


def messages(client, kind):
    client.flush()
    return [m.text for m in load_messages(client.log_file.name) if m.type == kind]


def eye_sample(i=0):
    return make_sample(i, x=500.0, y=400.0)


def test_calibrate_refuses_without_eye(client):
    start(client)
    for i in range(20):
        client.api.emit(make_sample(i, valid=False))  # samples arrive, but no eye is found
    with pytest.raises(NoEyeDetectedError, match="No eye detected"):
        client.calibrate(eye_timeout=0.2)
    assert ("calibrate", 0) not in client.api.calls
    assert any("No eye detected" in t for t in messages(client, "ERROR"))


def test_calibrate_proceeds_when_eye_found(client):
    start(client)
    client.api.emit(eye_sample())
    client.calibrate(eye_timeout=0.2)
    assert ("calibrate", 0) in client.api.calls


def test_eye_check_can_be_disabled(client):
    start(client)
    client.calibrate(eye_timeout=None)
    assert ("calibrate", 0) in client.api.calls


def test_validation_without_any_eye_is_an_error(client, monkeypatch):
    start(client)
    invalid = client.api.validate()[1]
    for p in invalid.pointsData:
        p.meanDeviationLeftDeg = p.meanDeviationLeftPx = client._sdk.ELInvalidValue
    monkeypatch.setattr(client.api, "validate", lambda: (client.api.ReturnValidate.SUCCESS, invalid))
    with pytest.raises(ValidationError, match="no eye"):
        client.validate()


def test_recording_without_eye_warns_loudly(client):
    start(client)
    client.start_recording()
    for i in range(50):
        client.api.emit(make_sample(i, valid=False))
    with pytest.warns(EyeLogicWarning, match="no eye"):
        client.stop_recording()
    assert any("no eye" in t for t in messages(client, "WARNING"))
    assert any("50 samples" in t and "0%" in t for t in messages(client, "INFO"))


def test_recording_without_samples_warns(client):
    start(client)
    client.start_recording()
    with pytest.warns(EyeLogicWarning, match="no samples"):
        client.stop_recording()


def test_normal_recording_reports_eye_share_without_warning(client):
    start(client)
    client.start_recording()
    for i in range(40):
        client.api.emit(eye_sample(i) if i % 4 else make_sample(i, valid=False))
    with warnings.catch_warnings():
        warnings.simplefilter("error", EyeLogicWarning)
        client.stop_recording()
    assert any("40 samples" in t and "75%" in t for t in messages(client, "INFO"))
    assert messages(client, "WARNING") == []
