"""Stage 1.5: deterministic forecasting. Costs $0 and runs before any LLM sees the data.

History alone cannot see next month's sale, and an LLM must never invent a date. So the forecast combines
two honest sources:

- **history**: recurring peaks Stage 1 found in the metrics (``WorkloadStats.peaks``), and
- **calendar**: business events the operator declared in ``config/calendar.yaml`` (working hours, Black
  Friday, Christmas, promotions), each with an expected traffic multiplier.

Every window becomes an expected total CPU, a pre-warm time (scale up *before* the rush, not during it)
and a replica count sized at the CPU target. Confidence is stated, never hidden: history is scored by how
often the peak recurred and how much history backs it; calendar events are operator-declared, so they score
high on timing and rest on the declared multiplier.

Weekly windows fit the KEDA cron subset and guardrails can enforce them. Dated events (a specific sale
day, a holiday week) cannot be expressed as a weekly cron, so they are forecast and priced only: the
recommendation must carry a follow-up to apply the timed scale-up before the date.
"""
from __future__ import annotations

import math
from datetime import date

from pydantic import BaseModel

from kubesage.config import BusinessCalendar, CalendarEvent
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import cron

DEFAULT_CPU_TARGET = 70  # percent of the CPU request a replica should carry at the forecast peak
HISTORY_PREWARM_MIN = 15
_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


class ForecastWindow(BaseModel):
    source: str  # "history" | "calendar"
    event: str  # peak description or calendar event name
    kind: str = "peak"  # calendar kind, or "peak" for history
    schedule: str  # human schedule, e.g. "Mon-Fri 09:00-17:00" or "2026-11-27 08:00-22:00"
    days: str | None = None  # cron day-of-week for weekly windows
    start_time: str  # "HH:MM" local, when the load window opens
    end_time: str
    prewarm_time: str  # "HH:MM" local, when scaling should start
    prewarm_minutes: int
    cron_start: str | None = None  # KEDA cron for the pre-warm; None when the event is dated, not weekly
    cron_end: str | None = None
    baseline_total_cpu: float
    expected_total_cpu: float
    multiplier: float  # expected / baseline
    replicas_needed: int
    confidence: float  # 0..1
    note: str = ""


class WorkloadForecast(BaseModel):
    workload: str
    baseline_total_cpu: float  # normal (median) total CPU, cores
    expected_peak_cpu: float  # highest expected total CPU across windows
    peak_multiplier: float
    replicas_needed: int  # replicas that cover the expected peak at the CPU target
    offpeak_replicas: int  # replicas that cover normal load: the scale-down target outside windows
    confidence: float
    windows: list[ForecastWindow] = []
    summary: str = ""


def _hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_hhmm(value: str) -> int:
    hour, _, minute = value.partition(":")
    return int(hour) * 60 + int(minute)


def _format_days(days: frozenset[int]) -> str:
    if len(days) == 7:
        return "*"
    ordered = sorted(days)
    ranges, start, prev = [], ordered[0], ordered[0]
    for day in ordered[1:]:
        if day == prev + 1:
            prev = day
            continue
        ranges.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = day
    ranges.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(ranges)


def _shift_days(days_field: str, offset: int) -> str:
    """Shift a cron day-of-week field by whole days (a pre-warm just before midnight belongs to the day before)."""
    if offset == 0:
        return days_field
    days = cron._days(days_field)
    return _format_days(frozenset((d + offset) % 7 for d in days))


def _cron_at(days_field: str, minutes_of_day: int) -> str:
    day_offset, minute = divmod(minutes_of_day, 24 * 60)
    return f"{minute % 60} {minute // 60} * * {_shift_days(days_field, day_offset)}"


def _days_label(days_field: str) -> str:
    if days_field == "*":
        return "Daily"
    if days_field == "1-5":
        return "Mon-Fri"
    if days_field == "0,6":
        return "Weekends"
    try:
        return ",".join(_DAY_NAMES[d] for d in sorted(cron._days(days_field)))
    except ValueError:
        return days_field


def _replicas(expected_total_cpu: float, per_pod_capacity: float) -> int:
    if expected_total_cpu <= 0:
        return 1
    return max(1, math.ceil(expected_total_cpu / per_pod_capacity - 1e-9))


def _per_pod_capacity(s: WorkloadStats) -> float:
    """CPU one replica should carry at the forecast peak: the request (or observed p95) at the CPU target."""
    request = s.cpu_request_cores or s.cpu_pod_p95 or 0.1
    return max(request, 0.01) * DEFAULT_CPU_TARGET / 100


def _baseline(s: WorkloadStats) -> float:
    hourly = [v for v in s.weekday_hourly_cpu + s.weekend_hourly_cpu if v > 0]
    if hourly:
        return float(sorted(hourly)[len(hourly) // 2])
    return round(s.cpu_total_p95, 3)


def _history_windows(s: WorkloadStats, baseline: float, capacity: float) -> list[ForecastWindow]:
    windows = []
    history_factor = min(1.0, max(s.window_days, 0.0) / 14)  # two weeks of history earns full trust
    for peak in s.peaks:
        expected = peak.peak_total_cpu
        prewarm_start = peak.start_hour * 60 - HISTORY_PREWARM_MIN
        windows.append(ForecastWindow(
            source="history", event=f"Recurring {peak.start_hour:02d}:00 peak", kind="peak",
            schedule=f"{_days_label(peak.days)} {peak.start_hour:02d}:00-{peak.end_hour:02d}:00",
            days=peak.days, start_time=f"{peak.start_hour:02d}:00", end_time=f"{peak.end_hour % 24:02d}:00",
            prewarm_time=_hhmm(prewarm_start), prewarm_minutes=HISTORY_PREWARM_MIN,
            cron_start=_cron_at(peak.days, prewarm_start), cron_end=_cron_at(peak.days, peak.end_hour * 60),
            baseline_total_cpu=round(baseline, 3), expected_total_cpu=round(expected, 3),
            multiplier=round(expected / baseline, 2) if baseline > 0 else 1.0,
            replicas_needed=_replicas(expected, capacity),
            confidence=round(min(0.95, peak.recurrence * (0.5 + 0.5 * history_factor)), 2),
            note=f"Seen on {peak.recurrence:.0%} of matching days in the metrics."))
    return windows


def _event_schedule(event: CalendarEvent, today: date) -> str:
    if event.weekly:
        return f"{_days_label(event.days or '*')} {event.start_time}-{event.end_time}"
    if event.date:
        label = event.date
        if event.repeat_yearly:
            try:
                when = date.fromisoformat(event.date).replace(year=today.year)
                if when < today:
                    when = when.replace(year=today.year + 1)
                label = when.isoformat()
            except ValueError:
                pass
        return f"{label} {event.start_time}-{event.end_time}"
    return f"{event.start_date} to {event.end_date}, {event.start_time}-{event.end_time} daily"


def _calendar_windows(s: WorkloadStats, calendar: BusinessCalendar, baseline: float, capacity: float,
                      today: date) -> list[ForecastWindow]:
    windows = []
    for event in calendar.for_workload(s.name):
        expected = baseline * event.traffic_multiplier
        # A declared rush must never forecast below a peak the metrics already prove happens: a sale day
        # still contains the normal rush, plus the sale. The stated multiplier is against normal load;
        # the window's multiplier is recomputed against the same baseline as every other window.
        if event.traffic_multiplier > 1 and s.peaks:
            expected = max(expected, max(p.peak_total_cpu for p in s.peaks))
        prewarm = event.prewarm_minutes if event.prewarm_minutes is not None else calendar.prewarm_minutes
        start_min = _parse_hhmm(event.start_time)
        weekly = event.weekly
        windows.append(ForecastWindow(
            source="calendar", event=event.name, kind=event.kind,
            schedule=_event_schedule(event, today),
            days=event.days if weekly else None,
            start_time=event.start_time, end_time=event.end_time,
            prewarm_time=_hhmm(start_min - prewarm), prewarm_minutes=prewarm,
            cron_start=_cron_at(event.days or "*", start_min - prewarm) if weekly else None,
            cron_end=_cron_at(event.days or "*", _parse_hhmm(event.end_time) + event.cooldown_minutes) if weekly else None,
            baseline_total_cpu=round(baseline, 3), expected_total_cpu=round(expected, 3),
            multiplier=round(expected / baseline, 2) if baseline > 0 else round(event.traffic_multiplier, 2),
            replicas_needed=_replicas(expected, capacity),
            # Declared by the operator: timing is trusted; the multiplier is their estimate.
            confidence=0.85 if weekly else 0.8,
            note=event.note or ("Declared in config/calendar.yaml." if weekly else
                                "Declared in config/calendar.yaml. Dated event: apply a timed scale-up before "
                                "the date; a weekly cron cannot express a one-off date.")))
    return windows


def forecast_workload(s: WorkloadStats, calendar: BusinessCalendar | None = None, *,
                      today: date | None = None) -> WorkloadForecast:
    """Expected load for one workload: when it will be busy, how busy, and how many replicas that needs."""
    calendar = calendar or BusinessCalendar()
    today = today or date.today()
    baseline = _baseline(s)
    capacity = _per_pod_capacity(s)
    windows = _history_windows(s, baseline, capacity) + _calendar_windows(s, calendar, baseline, capacity, today)
    # Strongest window first; on a tie a declared calendar event headlines over the history peak it contains.
    windows.sort(key=lambda w: (w.expected_total_cpu, w.source == "calendar"), reverse=True)
    expected_peak = max((w.expected_total_cpu for w in windows), default=round(s.cpu_total_p95, 3))
    replicas = max((w.replicas_needed for w in windows), default=_replicas(expected_peak, capacity))
    offpeak = _replicas(baseline, capacity)
    confidence = max((w.confidence for w in windows), default=0.5)
    summary = _summary(s, windows, baseline, expected_peak, replicas, offpeak)
    return WorkloadForecast(workload=s.id, baseline_total_cpu=round(baseline, 3),
                            expected_peak_cpu=round(expected_peak, 3),
                            peak_multiplier=round(expected_peak / baseline, 2) if baseline > 0 else 1.0,
                            replicas_needed=replicas, offpeak_replicas=offpeak,
                            confidence=round(confidence, 2), windows=windows, summary=summary)


def _summary(s: WorkloadStats, windows: list[ForecastWindow], baseline: float, peak: float,
             replicas: int, offpeak: int) -> str:
    if not windows:
        return (f"{s.name}: steady load around {baseline:.2f} cores with no recurring peak and no declared "
                f"event; {offpeak} replica(s) cover normal load.")
    top = windows[0]
    return (f"{s.name}: {top.event} ({top.schedule}) is expected to reach ~{peak:.2f} cores "
            f"({top.multiplier:.1f}x the {baseline:.2f}-core baseline), so pre-warm at {top.prewarm_time} "
            f"to {replicas} replica(s); outside the window {offpeak} replica(s) cover normal load.")


def build_forecasts(stats: list[WorkloadStats], calendar: BusinessCalendar | None = None, *,
                    today: date | None = None) -> list[WorkloadForecast]:
    """One forecast per workload, in input order. Pure and deterministic: no clock except `today`, no LLM."""
    return [forecast_workload(s, calendar, today=today) for s in stats]
