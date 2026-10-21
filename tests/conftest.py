"""Tests run against ``eyelogic_recorder.simulator`` in manual mode (no sample thread): tests call ``api.emit()``."""

import sys

import pytest

from eyelogic_recorder import simulator


@pytest.fixture
def fake_sdk(monkeypatch):
    pkg, mod = simulator.make_modules(rate_hz=0)
    monkeypatch.setitem(sys.modules, "eyelogic", pkg)
    monkeypatch.setitem(sys.modules, "eyelogic.ELApi", mod)
    return mod


@pytest.fixture
def client(fake_sdk, tmp_path):
    from eyelogic_recorder import EyeLogicClient

    c = EyeLogicClient("test", data_folder=str(tmp_path), buffer_size=100, writer_queue_size=4)
    yield c
    c.close()


@pytest.fixture
def roomy_client(fake_sdk, tmp_path):
    """A client whose buffer pool (200k samples) absorbs tests that emit at full CPU speed."""
    from eyelogic_recorder import EyeLogicClient

    c = EyeLogicClient("test", data_folder=str(tmp_path), buffer_size=100, writer_queue_size=2000)
    yield c
    c.close()
