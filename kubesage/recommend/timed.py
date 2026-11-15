"""Dated calendar events as KEDA cron triggers: the part a weekly cron window cannot express.

A Black Friday on 2026-11-27 is not "every Friday", and a model must not be the one to schedule it. KEDA's
cron scaler takes a full five-field cron, so a dated event becomes one more trigger on the workload's own
ScaledObject, with the day of the month and the month filled in:

    start: "30 7 27 11 *"    # pre-warm, 07:30 on 27 November
    end:   "0 23 27 11 *"    # the window's end plus its cooldown

A range of days (20-26 December, 09:00-21:00) becomes one trigger that opens and closes every day of the range;
a range that crosses a month end becomes one trigger per month.

Earlier versions rendered a pair of `kubectl scale` CronJobs instead. Those needed an image and RBAC that were
never shipped, were overruled by the autoscaler they scaled behind the back of, covered only the first day of a
range and had names the API rejects. A trigger on the ScaledObject has none of those problems: KEDA takes the
largest active trigger, so the event raises the floor for its hours and nothing else changes.

A cron repeats every year. For a one-off event that is wrong after the date, so the ScaledObject says so in an
annotation, and the next Analyze drops the trigger once the date has passed.
"""
from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel

from kubesage.forecast import ForecastWindow, WorkloadForecast


class DatedTrigger(BaseModel):
    event: str
    start: str  # five-field cron in the cluster timezone
    end: str
    replicas: int
    label: str  # for the annotation: what, when, and whether it is a one-off
    one_off: bool = False


def _minutes(value: str) -> int:
    hour, _, minute = value.partition(":")
    return int(hour) * 60 + int(minute)


def _days_by_month(first: date, last: date) -> list[tuple[int, int, int]]:
    """(month, first day, last day) for each calendar month a range touches, in order."""
    out, day = [], first
    while day <= last:
        month_end = (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        stop = min(last, month_end)
        out.append((day.month, day.day, stop.day))
        day = stop + timedelta(days=1)
    return out


def _window_triggers(window: ForecastWindow, today: date) -> list[DatedTrigger]:
    if window.source != "calendar" or window.cron_start is not None or not window.first_day or not window.last_day:
        return []
    first, last = date.fromisoformat(window.first_day), date.fromisoformat(window.last_day)
    if last < today and not window.repeat_yearly:
        return []  # a one-off that has passed: a cron would bring it back next year
    # The pre-warm and the cooldown stay inside the day: a window is never carried across midnight, where a
    # daily start/end pair would no longer bracket one day's rush.
    start = max(0, _minutes(window.start_time) - window.prewarm_minutes)
    end = min(23 * 60 + 59, _minutes(window.end_time) + window.cooldown_minutes)
    if end <= start:
        end = 23 * 60 + 59
    one_off = not window.repeat_yearly
    when = window.first_day if first == last else f"{window.first_day} to {window.last_day}"
    label = (f"{window.event}: {window.replicas_needed} replica{'' if window.replicas_needed == 1 else 's'}, {when}, {start // 60:02d}:{start % 60:02d}-"
             f"{end // 60:02d}:{end % 60:02d}" + (" (one-off: re-run Analyze after it, or it repeats next year)" if one_off
                                                 else " (yearly)"))
    out = []
    for month, d1, d2 in _days_by_month(first, last):
        days = str(d1) if d1 == d2 else f"{d1}-{d2}"
        out.append(DatedTrigger(event=window.event, start=f"{start % 60} {start // 60} {days} {month} *",
                                end=f"{end % 60} {end // 60} {days} {month} *", replicas=window.replicas_needed,
                                label=label, one_off=one_off))
    return out


def dated_triggers(forecast: WorkloadForecast | None, *, today: date | None = None) -> list[DatedTrigger]:
    """One trigger per dated calendar window and month. Weekly windows return nothing: the scaling plan owns them."""
    if forecast is None:
        return []
    today = today or date.today()
    return [t for window in forecast.windows for t in _window_triggers(window, today)]
