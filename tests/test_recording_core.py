"""Recording core under pressure (review by EyeLogic, 2026-10-15).

1. The sample callback must never wait: the EyeLogic server drops samples
   when the callback is slow. When no buffer is free, samples are dropped,
   counted, and reported as an error.
2. Stopping must not lose a chunk that a concurrent callback just filled.
3. Errors writing the message file must not pass silently.
4. flush() must wait until everything recorded so far is on disk.
"""

import threading
import time

import numpy as np
import pytest

import eyelogic_recorder
from eyelogic_recorder import load_messages, load_samples
from eyelogic_recorder.simulator import make_sample


def start(client):
    client.connect()
    client.start_tracking()


class StalledWrites:
    """Make the writer thread's file writes block until released."""

    def __init__(self, file, monkeypatch):
        self.release = threading.Event()
        real_write = file.write

        def write(data):
            self.release.wait()
            return real_write(data)

        monkeypatch.setattr(file, "write", write)


def emit_in_thread(api, indices, timeout=5.0):
    """Emit samples from another thread (like the SDK). Returns the per-sample callback durations."""
    durations = []

    def run():
        for i in indices:
            t0 = time.perf_counter()
            api.emit(make_sample(i))
            durations.append(time.perf_counter() - t0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "sample callback blocked (producer still running after {} s)".format(timeout)
    return durations


def test_callback_never_blocks_when_writer_stalls(client, monkeypatch):
    # client: buffer_size=100, writer_queue_size=4 -> 6 chunks = 600 samples of buffer
    start(client)
    client.start_recording()
    stall = StalledWrites(client.sample_file, monkeypatch)
    try:
        durations = emit_in_thread(client.api, range(2000))
        assert max(durations) < 0.01, "slowest callback took {:.1f} ms".format(1000 * max(durations))
    finally:
        stall.release.set()


def test_overflow_drops_counts_and_reports(client, monkeypatch):
    start(client)
    client.start_recording()
    stall = StalledWrites(client.sample_file, monkeypatch)
    emit_in_thread(client.api, range(2000))
    assert client.samples_dropped > 0
    with pytest.raises(eyelogic_recorder.RecordingOverflowError, match="dropped"):
        client.check_health()

    # Once the writer catches up, recording continues; nothing accepted is lost.
    stall.release.set()
    client.flush()  # wait for the writer; this returns its buffers to the pool
    emit_in_thread(client.api, range(2000, 2300))
    client.stop_recording()
    data = load_samples(client.sample_file.name)
    assert data.size == client.samples_written == client.samples_recorded
    assert client.samples_recorded + client.samples_dropped == 2300
    assert data["index"][-1] == 2299  # samples after the overflow were recorded
    assert np.all(np.diff(data["index"]) > 0)


def test_stop_does_not_lose_concurrently_filled_chunk(fake_sdk, tmp_path):
    # Small chunks make "chunk just filled" coincide with stop_recording() often.
    et = eyelogic_recorder.EyeLogicClient("race", data_folder=str(tmp_path), buffer_size=5, writer_queue_size=4)
    try:
        start(et)
        stop = threading.Event()

        def produce():  # ~10 kHz: far above any tracker, but within the writer's capacity
            i = 0
            while not stop.is_set():
                et.api.emit(make_sample(i))
                i += 1
                time.sleep(0.0001)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        for _ in range(200):
            et.start_recording()
            time.sleep(0.0005)
            et.stop_recording()
        stop.set()
        producer.join(5)
        assert et.samples_recorded > 0
        assert et.samples_dropped == 0
        assert et.samples_written == et.samples_recorded
        assert load_samples(et.sample_file.name).size == et.samples_recorded
    finally:
        et.close()


def test_flush_waits_for_queued_chunks(client, monkeypatch):
    start(client)
    client.start_recording()
    real_write = client.sample_file.write

    def slow_write(data):
        time.sleep(0.1)
        return real_write(data)

    monkeypatch.setattr(client.sample_file, "write", slow_write)
    emit_in_thread(client.api, range(350))  # 3 full chunks queued + 50 in the current one
    client.flush()
    assert load_samples(client.sample_file.name).size == 350


def test_message_write_error_is_reported(client, monkeypatch):
    error = getattr(eyelogic_recorder, "MessageWriteError", None)
    assert error is not None, "eyelogic_recorder.MessageWriteError missing"

    def broken(data):
        raise OSError("disk full")

    monkeypatch.setattr(client.log_file, "write", broken)
    client.send_message("TRIALID 1")
    with pytest.raises(error, match="TRIALID 1"):
        client.check_health()


def test_messages_still_written_normally(client):
    client.send_message("hello")
    client.flush()
    assert load_messages(client.log_file.name)[-1].text == "hello"


@pytest.fixture
def fsync_calls(monkeypatch):
    """Record the file descriptors passed to os.fsync (EyeLogic review: flush must reach the disk)."""
    import os

    calls = []
    real = os.fsync

    def spy(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return calls


def test_flush_syncs_both_files_to_disk(client, fsync_calls):
    start(client)
    client.start_recording()
    emit_in_thread(client.api, range(50))
    client.flush()
    assert client.sample_file.fileno() in fsync_calls
    assert client.log_file.fileno() in fsync_calls


def test_stop_recording_syncs_samples_to_disk(client, fsync_calls):
    start(client)
    client.start_recording()
    emit_in_thread(client.api, range(50))
    client.stop_recording()
    assert client.sample_file.fileno() in fsync_calls
