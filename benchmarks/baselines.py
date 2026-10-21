"""Recording strategies that write inside the SDK callback, for comparison with eyelogic-recorder.

Both are reconstructed from earlier versions of this code (repository history
before the asynchronous writer). For a fair timing comparison they write every
sample in the same CSV format as eyelogic-recorder; the historical chunk version also
dropped invalid samples and negative (off-screen) coordinates, which is a data
quality problem but not a timing one.
"""

import csv
import time
from ctypes import byref, memmove, sizeof

import numpy as np

from eyelogic_recorder.io import CSV_HEADER, GAZE_FIELDS, SAMPLE_DTYPE, format_rows


class RowInCallback:
    """One ``csv.writer.writerow`` per sample, inside the callback (earliest version)."""

    name = "row_in_callback"

    def __init__(self, file, sdk):
        self.file = file
        self.sdk = sdk
        self.writer = csv.writer(file)
        file.write(",".join(CSV_HEADER) + "\n")

    def callback(self, ptr):
        s = ptr.contents
        now = time.time()
        if s.porRawX == self.sdk.ELInvalidValue:
            self.writer.writerow([s.timestampMicroSec, s.index] + ["NA"] * 16 + [now])
        else:
            self.writer.writerow([
                s.timestampMicroSec, s.index, s.porRawX, s.porRawY, s.porFilteredX, s.porFilteredY,
                s.porLeftX, s.porLeftY, s.eyePositionLeftX, s.eyePositionLeftY, s.eyePositionLeftZ,
                s.pupilRadiusLeft, s.porRightX, s.porRightY, s.eyePositionRightX, s.eyePositionRightY,
                s.eyePositionRightZ, s.pupilRadiusRight, now,
            ])

    def close(self):
        self.file.flush()


class ChunkInCallback:
    """Fill a NumPy chunk; when full, format and write it inside the callback (2024 version)."""

    name = "chunk_in_callback"

    def __init__(self, file, sdk, buffer_size=1000):
        self.file = file
        self.sdk = sdk
        self.buffer = np.empty(buffer_size, dtype=SAMPLE_DTYPE)
        self.count = 0
        file.write(",".join(CSV_HEADER) + "\n")

    def callback(self, ptr):
        s = self.sdk.ELGazeSample()
        memmove(byref(s), ptr, sizeof(s))
        self.buffer[self.count] = (
            s.timestampMicroSec, s.index, s.porRawX, s.porRawY, s.porFilteredX, s.porFilteredY,
            s.porLeftX, s.porLeftY, s.eyePositionLeftX, s.eyePositionLeftY, s.eyePositionLeftZ,
            s.pupilRadiusLeft, s.porRightX, s.porRightY, s.eyePositionRightX, s.eyePositionRightY,
            s.eyePositionRightZ, s.pupilRadiusRight, time.time(),
        )
        self.count += 1
        if self.count == len(self.buffer):
            self.flush()

    def flush(self):
        rows = self.buffer[: self.count]
        for field in GAZE_FIELDS:
            column = rows[field]
            column[column == self.sdk.ELInvalidValue] = np.nan
        self.file.write(format_rows(rows))
        self.count = 0

    def close(self):
        self.flush()
        self.file.flush()


class StallingFile:
    """File wrapper that blocks ``write`` for ``stall_s`` once every ``every_s`` seconds.

    Models a slow or busy disk (antivirus scan, network drive, sync client).
    """

    def __init__(self, file, stall_s, every_s):
        self._file = file
        self._stall_s = stall_s
        self._every_s = every_s
        self._next = time.perf_counter() + every_s
        self.stalls = 0

    def write(self, data):
        if self._stall_s > 0 and time.perf_counter() >= self._next:
            time.sleep(self._stall_s)
            self.stalls += 1
            self._next = time.perf_counter() + self._every_s
        return self._file.write(data)

    def __getattr__(self, name):
        return getattr(self._file, name)
