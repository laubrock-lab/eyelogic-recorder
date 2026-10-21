import time

import numpy as np

import eyelogic_recorder
from eyelogic_recorder import simulator


def test_threaded_simulator_records_at_rate(monkeypatch, tmp_path):
    import sys
    pkg, mod = simulator.make_modules(rate_hz=250, record_timing=True)
    monkeypatch.setitem(sys.modules, "eyelogic", pkg)
    monkeypatch.setitem(sys.modules, "eyelogic.ELApi", mod)
    with eyelogic_recorder.EyeLogicClient("sim", data_folder=str(tmp_path)) as et:
        et.connect()
        et.start_tracking()
        et.start_recording()
        time.sleep(1.0)
        et.stop_recording()
        emitted_during = et.samples_written
        path = et.sample_file.name
        api = et.api
    data = eyelogic_recorder.load_samples(path)
    assert len(data) == emitted_during
    assert 200 <= len(data) <= 260  # ~250 Hz for 1 s
    assert np.all(np.diff(data["index"]) == 1)
    isi_ms = np.diff(data["timestampMicroSec"]) / 1000
    assert np.allclose(isi_ms, 4.0, atol=0.01)  # nominal timestamps
    assert len(api.timing) == api.emitted


def test_position_source_and_blinks(monkeypatch, tmp_path):
    import sys
    gaze = {"pos": (100.0, 200.0)}
    pkg, mod = simulator.make_modules(rate_hz=500, position=lambda: gaze["pos"])
    monkeypatch.setitem(sys.modules, "eyelogic", pkg)
    monkeypatch.setitem(sys.modules, "eyelogic.ELApi", mod)
    with eyelogic_recorder.EyeLogicClient("sim", data_folder=str(tmp_path)) as et:
        et.connect()
        et.start_tracking()
        time.sleep(0.05)
        assert et.get_position() == (100.0, 200.0)
        gaze["pos"] = None
        time.sleep(0.05)
        assert et.get_position() is None
