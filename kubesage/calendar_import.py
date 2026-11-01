"""Import business events from an iCalendar (.ics) export — Google/Outlook/holiday calendars.

The business calendar stays the only source of future dates; this just saves operators re-typing
events they already keep in a real calendar. Imported events are drafts: the multiplier is a
placeholder (an .ics file knows *when*, never *how much* traffic), and the note says so. Nothing is
written anywhere by this module — persisting still goes through the approved append path.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from kubesage.config import CalendarEvent

_KIND_HINTS = (("sale", "sale"), ("black friday", "sale"), ("promotion", "promotion"), ("promo", "promotion"),
               ("holiday", "holiday"), ("christmas", "holiday"), ("launch", "other"))


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:  # RFC 5545 folding: leading whitespace continues the line
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)  # unstripped: a space before a fold is content, not padding
    return lines


def _prop(line: str) -> tuple[str, dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    parsed = {}
    for p in params:
        k, _, v = p.partition("=")
        parsed[k.strip().upper()] = v.strip()
    return name.strip().upper(), parsed, value.strip()


def _moment(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """(wall-clock moment, all-day?) — TZID/Z are accepted but the wall time is taken as local,
    matching how calendar.yaml times are interpreted."""
    value = value.strip().rstrip("Z")
    if params.get("VALUE", "").upper() == "DATE" or "T" not in value:
        return datetime.strptime(value, "%Y%m%d"), True
    return datetime.strptime(value, "%Y%m%dT%H%M%S"), False


def _kind(summary: str) -> str:
    low = summary.lower()
    for hint, kind in _KIND_HINTS:
        if hint in low:
            return kind
    return "other"


def parse_ics(text: str, *, default_multiplier: float = 1.5) -> tuple[list[CalendarEvent], list[str]]:
    """Parse VEVENTs into validated calendar events. Returns (events, skipped-reasons)."""
    events: list[CalendarEvent] = []
    skipped: list[str] = []
    block: dict[str, tuple[dict[str, str], str]] = {}
    in_event = False
    for line in _unfold(text):
        if line.strip() == "BEGIN:VEVENT":
            block, in_event = {}, True
            continue
        if line.strip() == "END:VEVENT":
            in_event = False
            event = _to_event(block, default_multiplier, skipped)
            if event is not None:
                events.append(event)
            continue
        if in_event:
            name, params, value = _prop(line)
            block[name] = (params, value)
    return events, skipped


def _to_event(block: dict[str, tuple[dict[str, str], str]], default_multiplier: float,
              skipped: list[str]) -> CalendarEvent | None:
    summary = block.get("SUMMARY", ({}, ""))[1].strip()
    label = summary or "<unnamed>"
    if "DTSTART" not in block:
        skipped.append(f"{label}: no DTSTART, cannot place it on the calendar")
        return None
    try:
        start, all_day = _moment(*_unpack(block["DTSTART"]))
        end = start + timedelta(days=1)
        if "DTEND" in block:
            end, _ = _moment(*_unpack(block["DTEND"]))
    except ValueError:
        skipped.append(f"{label}: unparseable DTSTART/DTEND")
        return None
    if not summary:
        skipped.append("event with no SUMMARY, cannot name it")
        return None
    rrule = block.get("RRULE", ({}, ""))[1].upper()
    yearly = "FREQ=YEARLY" in rrule
    note = (f"Imported from iCalendar ({start.year}). Draft: set traffic_multiplier from your own "
            "expectations before relying on this forecast.")
    fields: dict = dict(name=summary, kind=_kind(summary), traffic_multiplier=default_multiplier, note=note)
    last_day = end - timedelta(days=1) if all_day else end  # ICS all-day DTEND is exclusive
    if last_day.date() > start.date():  # multi-day span -> yearly range, like Christmas week
        fields.update(start_date=start.strftime("%m-%d"), end_date=last_day.strftime("%m-%d"),
                      start_time="00:00" if all_day else start.strftime("%H:%M"),
                      end_time="23:59" if all_day else end.strftime("%H:%M"))
    else:
        fields.update(date=start.strftime("%Y-%m-%d"), repeat_yearly=yearly,
                      start_time="00:00" if all_day else start.strftime("%H:%M"),
                      end_time="23:59" if all_day else end.strftime("%H:%M"))
    try:
        return CalendarEvent(**fields)
    except ValueError as exc:
        skipped.append(f"{label}: {exc}")
        return None


def _unpack(prop: tuple[dict[str, str], str]) -> tuple[str, dict[str, str]]:
    params, value = prop
    return value, params
