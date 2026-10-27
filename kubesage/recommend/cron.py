"""The small cron subset KubeSage accepts from models: "M H * * DOW" with numeric minute and hour."""
from __future__ import annotations

from bisect import bisect_right

MINUTES_PER_WEEK = 7 * 24 * 60


def parse(expr: str) -> tuple[int, int, frozenset[int]]:
    """Return (minute, hour, days-of-week with Sunday=0). Raises ValueError outside the subset."""
    parts = expr.split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*":
        raise ValueError(f"unsupported cron {expr!r}")
    minute, hour = int(parts[0]), int(parts[1])
    if not (0 <= minute < 60 and 0 <= hour < 24):
        raise ValueError(f"minute/hour out of range in {expr!r}")
    return minute, hour, _days(parts[4])


def _days(field: str) -> frozenset[int]:
    if field == "*":
        return frozenset(range(7))
    days: set[int] = set()
    for part in field.split(","):
        lo, dash, hi = part.partition("-")
        if dash and not hi:
            raise ValueError(f"bad day-of-week {field!r}")
        lo_i, hi_i = int(lo), int(hi or lo)
        # KEDA uses robfig/cron, whose day-of-week range is 0..6 (Sunday=0).
        if not (0 <= lo_i <= hi_i <= 6):
            raise ValueError(f"bad day-of-week {field!r}")
        days.update(range(lo_i, hi_i + 1))
    return frozenset(days)


def active_minutes(start: str, end: str) -> frozenset[int]:
    """Active minutes in a nominal wall-clock week, matching KEDA's next-start/next-end comparison.

    Keeping both day fields matters for overnight and weekend windows. A minute at the start is active;
    a minute at the end is not. DST changes are not included in this weekly cost estimate.
    """
    sm, sh, start_days = parse(start)
    em, eh, end_days = parse(end)
    starts = sorted(d * 1440 + sh * 60 + sm for d in start_days)
    ends = sorted(d * 1440 + eh * 60 + em for d in end_days)
    if starts == ends:
        raise ValueError("cron start and end must describe different times")
    starts.append(starts[0] + MINUTES_PER_WEEK)
    ends.append(ends[0] + MINUTES_PER_WEEK)
    return frozenset(m for m in range(MINUTES_PER_WEEK)
                     if ends[bisect_right(ends, m)] <= starts[bisect_right(starts, m)])


def window_hours_per_week(start: str, end: str) -> float:
    return len(active_minutes(start, end)) / 60
