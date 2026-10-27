"""The small cron subset KubeSage accepts from models: "M H * * DOW" with numeric minute and hour."""
from __future__ import annotations


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
        lo, _, hi = part.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        if not (0 <= lo_i <= hi_i <= 7):
            raise ValueError(f"bad day-of-week {field!r}")
        days.update(d % 7 for d in range(lo_i, hi_i + 1))
    return frozenset(days)


def window_hours_per_week(start: str, end: str) -> float:
    sm, sh, days = parse(start)
    em, eh, _ = parse(end)
    duration_min = ((eh * 60 + em) - (sh * 60 + sm)) % 1440
    return duration_min / 60 * len(days)
