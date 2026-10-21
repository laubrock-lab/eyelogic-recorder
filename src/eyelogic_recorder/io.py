"""File formats written by ``EyeLogicClient`` and readers for them.

Samples: ``<name>_<YYYYmmdd_HHMMSS>.csv``, comma-separated, one row per gaze
sample (invalid samples included, with ``NA`` in the gaze/eye columns).

Messages: ``<name>_<YYYYmmdd_HHMMSS>.tsv``, tab-separated with header
``SysTime  Type  Text``. ``Type`` is ``MSG`` for user messages
(:meth:`EyeLogicClient.send_message`), ``EVENT`` for device events,
``INFO`` for client status lines, ``WARNING`` for data-quality problems and
``ERROR`` for failures (connect,
calibration, validation, ...). ``SysTime`` uses the same clock as the
``SysTime`` column of the sample file, so messages can be aligned with samples.
"""

from __future__ import annotations

import csv
import warnings
from typing import List, NamedTuple

import numpy as np

SAMPLE_DTYPE = [
    ("timestampMicroSec", np.int64),
    ("index", np.int32),
    ("porRawX", np.float64),
    ("porRawY", np.float64),
    ("porFilteredX", np.float64),
    ("porFilteredY", np.float64),
    ("porLeftX", np.float64),
    ("porLeftY", np.float64),
    ("eyePositionLeftX", np.float64),
    ("eyePositionLeftY", np.float64),
    ("eyePositionLeftZ", np.float64),
    ("pupilRadiusLeft", np.float64),
    ("porRightX", np.float64),
    ("porRightY", np.float64),
    ("eyePositionRightX", np.float64),
    ("eyePositionRightY", np.float64),
    ("eyePositionRightZ", np.float64),
    ("pupilRadiusRight", np.float64),
    ("sysTime", np.float64),
]

# Fields copied from the SDK sample; everything except the trailing sysTime.
SDK_FIELDS = [name for name, _ in SAMPLE_DTYPE[:-1]]

# Fields that carry the SDK's invalid-value marker when the eye is not tracked.
GAZE_FIELDS = SDK_FIELDS[2:]

CSV_HEADER = [
    "ETTime",
    "Index",
    "x",
    "y",
    "x.filtered",
    "y.filtered",
    "x.left",
    "y.left",
    "eyePositionLeftX",
    "eyePositionLeftY",
    "eyePositionLeftZ",
    "pr.left",
    "x.right",
    "y.right",
    "eyePositionRightX",
    "eyePositionRightY",
    "eyePositionRightZ",
    "pr.right",
    "SysTime",
]

CSV_FMT = ["%d", "%d"] + ["%.6f"] * 16 + ["%.6f"]

MESSAGE_HEADER = ["SysTime", "Type", "Text"]

_ROW_FMT = ",".join(CSV_FMT) + "\n"


def format_rows(chunk: np.ndarray) -> str:
    """Format sample rows as CSV text; NaN is written as ``NA``."""
    text = "".join(_ROW_FMT % tuple(row) for row in chunk.tolist())
    return text.replace("nan", "NA")


def load_samples(path: str) -> np.ndarray:
    """Read a sample CSV into a structured array (field names as in ``SAMPLE_DTYPE``).

    ``NA`` values are returned as ``NaN``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # header-only file
        data = np.genfromtxt(
            path,
            delimiter=",",
            skip_header=1,
            dtype=SAMPLE_DTYPE,
            missing_values="NA",
            filling_values=np.nan,
            autostrip=True,
        )
    if data.size == 0:
        return np.empty(0, dtype=SAMPLE_DTYPE)
    return np.atleast_1d(data)  # a single row comes back 0-d


class Message(NamedTuple):
    sys_time: float
    type: str
    text: str


def load_messages(path: str) -> List[Message]:
    """Read a message TSV into a list of ``Message(sys_time, type, text)``."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        next(reader)  # header
        return [Message(float(t), typ, text) for t, typ, text in reader]
