"""iCalendar import: real calendar exports become validated, clearly-draft calendar events."""
from kubesage.calendar_import import parse_ics
from kubesage.config import BusinessCalendar

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Black Friday sale
DTSTART;VALUE=DATE:20261127
DTEND;VALUE=DATE:20261128
RRULE:FREQ=YEARLY
END:VEVENT
BEGIN:VEVENT
SUMMARY:Flash promotion evening
DTSTART:20261205T180000
DTEND:20261205T235900
END:VEVENT
BEGIN:VEVENT
SUMMARY:Christmas shopping peak
DTSTART;VALUE=DATE:20261220
DTEND;VALUE=DATE:20261227
END:VEVENT
BEGIN:VEVENT
SUMMARY:Dateless mystery
END:VEVENT
END:VCALENDAR
"""


def test_parse_events_and_skips():
    events, skipped = parse_ics(ICS)
    by_name = {e.name: e for e in events}
    assert set(by_name) == {"Black Friday sale", "Flash promotion evening", "Christmas shopping peak"}
    assert len(skipped) == 1 and "Dateless mystery" in skipped[0]

    bf = by_name["Black Friday sale"]
    assert bf.date == "2026-11-27" and bf.repeat_yearly and bf.kind == "sale"
    assert (bf.start_time, bf.end_time) == ("00:00", "23:59")  # all-day

    promo = by_name["Flash promotion evening"]
    assert promo.date == "2026-12-05" and promo.kind == "promotion"
    assert (promo.start_time, promo.end_time) == ("18:00", "23:59")

    xmas = by_name["Christmas shopping peak"]  # exclusive DTEND 27th -> last day 26th
    assert (xmas.start_date, xmas.end_date) == ("12-20", "12-26") and xmas.kind == "holiday"


def test_imports_are_drafts_that_validate_as_a_calendar():
    events, _ = parse_ics(ICS)
    cal = BusinessCalendar(events=events)  # same validation the loader applies
    assert len(cal.for_workload("anything")) == 3
    assert all("Draft" in e.note for e in events)
    assert all(e.traffic_multiplier == 1.5 for e in events)  # placeholder, never an invented forecast


def test_folded_summary_lines_unfold():
    text = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Very long end of year \n sale event\n"
            "DTSTART;VALUE=DATE:20261231\nEND:VEVENT\nEND:VCALENDAR\n")
    events, skipped = parse_ics(text)
    assert not skipped and events[0].name == "Very long end of year sale event"
    assert events[0].date == "2026-12-31"
