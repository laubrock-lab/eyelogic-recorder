import threading

import numpy as np
import pytest

import eyelogic_recorder
from eyelogic_recorder import (
    ConnectError,
    DeviceDisconnectedError,
    WriterError,
    load_messages,
    load_samples,
)

from eyelogic_recorder.simulator import DeviceEvent, SimulatedELApi, make_sample


def start(client):
    client.connect()
    client.find_tracker()
    client.start_tracking()


def test_records_all_samples_including_invalid_as_nan(roomy_client):
    start(roomy_client)
    roomy_client.start_recording()
    n = 1050  # > 10 full chunks plus a partial one
    for i in range(n):
        roomy_client.api.emit(make_sample(i, valid=(i % 20 != 0)))
    roomy_client.stop_recording()

    data = load_samples(roomy_client.sample_file.name)
    assert len(data) == n == roomy_client.samples_written
    assert np.array_equal(data["index"], np.arange(n))
    invalid = data["index"] % 20 == 0
    assert np.isnan(data["porRawX"][invalid]).all()
    assert np.isnan(data["pupilRadiusRight"][invalid]).all()
    assert np.array_equal(data["porFilteredX"][~invalid], data["index"][~invalid].astype(float))
    assert (data["timestampMicroSec"] > 0).all()
    assert not np.isnan(data["sysTime"]).any()


def test_na_written_for_invalid_values(client):
    start(client)
    client.start_recording()
    client.api.emit(make_sample(0, valid=False))
    with pytest.warns(eyelogic_recorder.EyeLogicWarning):  # a recording without any eye is flagged
        client.stop_recording()
    with open(client.sample_file.name) as f:
        header, row = f.read().splitlines()
    assert header.startswith("ETTime,Index,x,y,x.filtered")
    assert row.split(",")[2] == "NA"


def test_concurrent_producer_loses_nothing(roomy_client):
    start(roomy_client)
    roomy_client.start_recording()
    n = 100_000

    def produce():
        for i in range(n):
            roomy_client.api.emit(make_sample(i, valid=(i % 20 != 0)))

    t = threading.Thread(target=produce)
    t.start()
    t.join()
    roomy_client.stop_recording()
    data = load_samples(roomy_client.sample_file.name)
    assert len(data) == n
    assert np.array_equal(data["index"], np.arange(n))


def test_samples_outside_recording_update_position_but_are_not_written(client):
    start(client)
    client.api.emit(make_sample(7))
    assert client.get_position() == (7.0, 7.0)
    assert client.get_position(as_array=True).dtype == np.float32
    client.api.emit(make_sample(8, valid=False))
    assert client.get_position() is None
    client.flush()
    assert load_samples(client.sample_file.name).size == 0


def test_multiple_recordings_append(client):
    start(client)
    for trial in range(3):
        client.start_recording()
        client.send_message("TRIALID {}".format(trial))
        for i in range(trial * 10, trial * 10 + 10):
            client.api.emit(make_sample(i))
        client.send_message("TRIAL_END")
        client.stop_recording()
    assert len(load_samples(client.sample_file.name)) == 30
    client.flush()
    msgs = [m.text for m in load_messages(client.log_file.name) if m.type == "MSG"]
    assert msgs == ["TRIALID 0", "TRIAL_END", "TRIALID 1", "TRIAL_END", "TRIALID 2", "TRIAL_END"]


def test_message_sanitizes_tabs_and_newlines(client):
    client.send_message("a\tb\nc")
    client.flush()
    assert load_messages(client.log_file.name)[-1].text == "a b c"


def test_device_disconnect_stops_recording_and_raises(client):
    start(client)
    client.start_recording()
    for i in range(150):
        client.api.emit(make_sample(i))
    client.api.fire(DeviceEvent.DEVICE_DISCONNECTED)
    assert len(load_samples(client.sample_file.name)) == 150  # flushed on the event
    with pytest.raises(DeviceDisconnectedError):
        client.get_position()
    with pytest.raises(eyelogic_recorder.EyeLogicError):
        client.start_recording()


def test_own_stop_tracking_and_disconnect_are_not_errors(client):
    start(client)
    client.stop_tracking()
    client.disconnect()
    client.check_health()
    events = [m.text for m in load_messages_after_flush(client) if m.type == "EVENT"]
    assert events == ["TRACKING_STOPPED", "CONNECTION_CLOSED"]


def load_messages_after_flush(client):
    client.flush()
    return load_messages(client.log_file.name)


def test_writer_failure_surfaces(client, monkeypatch):
    start(client)
    client.start_recording()

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(client.sample_file, "write", broken)
    for i in range(100):  # one full chunk -> writer fails
        client.api.emit(make_sample(i))
    with pytest.raises(WriterError):
        client.stop_recording()
    with pytest.raises(WriterError):
        client.check_health()


def test_connect_failure(client):
    client.api.connect_result = SimulatedELApi.ReturnConnect.TIMEOUT
    with pytest.raises(ConnectError):
        client.connect()


def test_validate_returns_points_with_none_for_untracked_eye(client):
    start(client)
    points = client.validate()
    assert len(points) == 4
    assert (points[0].x_px, points[0].y_px) == (480.0, 270.0)
    assert points[0].left_deg == 0.3
    assert points[0].right_deg is None and points[0].right_px is None


def test_close_is_idempotent_and_cleans_up(client):
    start(client)
    client.start_recording()
    client.api.emit(make_sample(0))
    client.close()
    client.close()
    assert client.sample_file.closed and client.log_file.closed
    assert "unrequestTracking" in client.api.calls and "disconnect" in client.api.calls
    assert len(load_samples(client.sample_file.name)) == 1


def test_context_manager(fake_sdk, tmp_path):
    with eyelogic_recorder.EyeLogicClient("ctx", data_folder=str(tmp_path)) as et:
        start(et)
    assert et.sample_file.closed


def test_unique_filenames_within_same_second(fake_sdk, tmp_path):
    a = eyelogic_recorder.EyeLogicClient("same", data_folder=str(tmp_path))
    b = eyelogic_recorder.EyeLogicClient("same", data_folder=str(tmp_path))
    try:
        assert a.sample_file.name != b.sample_file.name
        assert a.log_file.name != b.log_file.name
    finally:
        a.close()
        b.close()


def test_missing_sdk_gives_helpful_error(monkeypatch, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "eyelogic", None)
    with pytest.raises(ImportError, match="eyelogic-sdk"):
        eyelogic_recorder.EyeLogicClient("x", data_folder=str(tmp_path))


def test_position_none_when_filtered_invalid(client, fake_sdk):
    start(client)
    s = make_sample(3)
    s.porFilteredX = fake_sdk.ELInvalidValue
    client.api.emit(s)
    assert client.get_position() is None


def test_close_reports_writer_failure_but_still_cleans_up(client, monkeypatch):
    start(client)
    client.start_recording()
    client.api.emit(make_sample(0))
    real_file = client.sample_file

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(real_file, "write", broken)
    with pytest.raises(WriterError):
        client.close()
    assert client.log_file.closed and real_file.closed
    assert "disconnect" in client.api.calls


def test_context_manager_does_not_mask_user_exception(fake_sdk, tmp_path, monkeypatch):
    with pytest.raises(KeyError):
        with eyelogic_recorder.EyeLogicClient("ctx", data_folder=str(tmp_path)) as et:
            start(et)
            et.start_recording()
            et.api.emit(make_sample(0))
            monkeypatch.setattr(et.sample_file, "write", lambda *a: (_ for _ in ()).throw(OSError("x")))
            raise KeyError("user error")


def test_last_sample_kept_when_por_invalid(client, fake_sdk):
    # Validity is per vector (EyeLogic): e.g. an eye found but not calibrated has a
    # valid eye position and an invalid point of regard. The sample is still kept.
    start(client)
    s = make_sample(5, valid=False)
    s.eyePositionLeftX, s.eyePositionLeftY, s.eyePositionLeftZ = -30.0, 1.0, 600.0
    client.api.emit(s)
    assert client.get_position() is None
    assert client.last_sample is not None
    assert client.last_sample.index == 5 and client.last_sample.eyePositionLeftZ == 600.0
    assert client.last_sample.porRawX == fake_sdk.ELInvalidValue


def test_older_sdk_is_rejected_clearly(monkeypatch, tmp_path):
    import sys
    import types

    from eyelogic_recorder import simulator

    pkg, mod = simulator.make_modules(rate_hz=0)
    old = types.ModuleType("eyelogic.ELApi")
    for name in ("ELApi", "ELGazeSample", "ELInvalidValue", "GazeSampleCallback"):
        setattr(old, name, getattr(mod, name))
    old.ELEvent = mod.ELDeviceEvent  # SDK 1.1 names only
    monkeypatch.setitem(sys.modules, "eyelogic", pkg)
    monkeypatch.setitem(sys.modules, "eyelogic.ELApi", old)
    with pytest.raises(ImportError, match=">= 1.2"):
        eyelogic_recorder.EyeLogicClient("x", data_folder=str(tmp_path))
