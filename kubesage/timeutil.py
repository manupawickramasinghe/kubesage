"""Local-time bucketing of epoch timestamps (hour of day, weekday), shared by simulator and analyzer."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np


def local_parts(ts: np.ndarray, tz: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (fractional local hour 0-24, weekday Mon=0, local day index from the first sample)."""
    zone = ZoneInfo(tz)
    # UTC offsets only change on hour boundaries, so resolve them once per hour instead of per sample.
    hours, inverse = np.unique(np.asarray(ts, dtype=np.int64) // 3600, return_inverse=True)
    offsets = np.array([datetime.fromtimestamp(int(h) * 3600, zone).utcoffset().total_seconds() for h in hours])
    local = np.asarray(ts, dtype=np.float64) + offsets[inverse]
    days = np.floor_divide(local, 86400)
    hour = (local - days * 86400) / 3600
    weekday = ((days + 3) % 7).astype(int)  # 1970-01-01 was a Thursday
    return hour, weekday, (days - days[0]).astype(int)
