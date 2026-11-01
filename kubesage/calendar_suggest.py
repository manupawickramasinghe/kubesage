"""Calendar suggestions: propose events from the metrics, but a human approves before anything is written.

The rule from calendar.md stands — the calendar is operator-declared and models never invent events.
This module lets evidence *ask* for an event: a recurring peak with no matching calendar window becomes
a suggested CalendarEvent. Suggestions change nothing on their own. `append_event` refuses to write
unless `approved=True` is passed explicitly, and it writes through the same pydantic validation as the
loader, so an approved suggestion can never produce a calendar the loader would reject.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from kubesage.config import BusinessCalendar, CalendarEvent
from kubesage.forecast import _baseline
from kubesage.metrics.stats import WorkloadStats


def suggest_events(stats: list[WorkloadStats], calendar: BusinessCalendar | None = None) -> list[CalendarEvent]:
    """One suggestion per recurring peak no declared weekly event already covers for that workload."""
    calendar = calendar or BusinessCalendar()
    out: list[CalendarEvent] = []
    for s in stats:
        covered = {(e.days, e.start_time) for e in calendar.for_workload(s.name) if e.weekly}
        baseline = _baseline(s)
        for peak in s.peaks:
            if any(days == peak.days for days, _ in covered):
                continue
            out.append(CalendarEvent(
                name=f"Suggested: {s.name} {peak.start_hour:02d}:00 peak",
                kind="working-hours", days=peak.days,
                start_time=f"{peak.start_hour:02d}:00", end_time=f"{peak.end_hour % 24:02d}:00",
                traffic_multiplier=round(peak.peak_total_cpu / baseline, 2) if baseline > 0 else 1.0,
                prewarm_minutes=15, workloads=[s.name],
                note=(f"SUGGESTED from metrics, not yet approved: peak recurs on {peak.recurrence:.0%} of "
                      f"matching days at ~{peak.peak_total_cpu:.2f} cores vs a {baseline:.2f}-core baseline. "
                      "Approve in config/calendar.yaml only if this matches a real business pattern.")))
    return out


def append_event(path: str | Path, event: CalendarEvent, *, approved: bool) -> BusinessCalendar:
    """Append one event to a calendar file. Refuses without explicit human approval."""
    if not approved:
        raise PermissionError("calendar events are operator-declared: pass approved=True only after a "
                              "human has reviewed this suggestion")
    path = Path(path)
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    calendar = BusinessCalendar.model_validate(data or {})
    if any(e.name == event.name for e in calendar.events):
        raise ValueError(f"calendar already has an event named {event.name!r}")
    calendar.events.append(event)
    validated = BusinessCalendar.model_validate(calendar.model_dump())  # never write what the loader rejects
    path.write_text(yaml.safe_dump(validated.model_dump(exclude_none=True), sort_keys=False))
    return validated
