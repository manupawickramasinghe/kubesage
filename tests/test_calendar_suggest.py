"""Calendar suggestions: evidence may propose, only a human may dispose."""
import pytest
import yaml

from kubesage.calendar_suggest import append_event, suggest_events
from kubesage.config import BusinessCalendar, load_calendar


def test_peaks_without_a_calendar_become_suggestions(stats):
    suggestions = suggest_events([stats["checkout-web"]], BusinessCalendar())
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.days == "1-5" and s.start_time == "09:00" and s.traffic_multiplier > 1
    assert "not yet approved" in s.note


def test_declared_working_hours_suppress_the_duplicate_suggestion(stats):
    cal = load_calendar()  # checkout-web already has Mon-Fri working hours declared
    assert suggest_events([stats["checkout-web"]], cal) == []


def test_appending_refuses_without_approval(tmp_path, stats):
    event = suggest_events([stats["checkout-web"]], BusinessCalendar())[0]
    target = tmp_path / "calendar.yaml"
    with pytest.raises(PermissionError):
        append_event(target, event, approved=False)
    assert not target.exists()  # a refused approval writes nothing, ever


def test_approved_append_round_trips_through_the_loader(tmp_path, stats):
    event = suggest_events([stats["checkout-web"]], BusinessCalendar())[0]
    target = tmp_path / "calendar.yaml"
    written = append_event(target, event, approved=True)
    reloaded = BusinessCalendar.model_validate(yaml.safe_load(target.read_text()))
    assert reloaded.events[-1].name == event.name and written.events[-1].days == "1-5"
    with pytest.raises(ValueError):
        append_event(target, event, approved=True)  # no duplicates
