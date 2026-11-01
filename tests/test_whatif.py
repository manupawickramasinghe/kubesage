"""What-if simulation: vary the sale multiplier, get replicas and money — no LLM, no cluster."""
import pytest

from kubesage.config import load_calendar, load_skus
from kubesage.whatif import simulate


def test_bigger_sale_needs_more_replicas_and_costs_more(stats):
    catalog, cal = load_skus(), load_calendar()
    small = simulate(list(stats.values()), cal, multiplier=2.0, catalog=catalog, event_name="Black Friday sale")
    big = simulate(list(stats.values()), cal, multiplier=5.0, catalog=catalog, event_name="Black Friday sale")
    assert small and big
    for a, b in zip(small, big):
        assert b.replicas_needed >= a.replicas_needed
        assert b.monthly_at_peak_usd >= a.monthly_at_peak_usd


def test_scheduling_a_peak_beats_holding_it_all_month(stats):
    rows = simulate(list(stats.values()), load_calendar(), multiplier=3.0, catalog=load_skus())
    weekly = [r for r in rows if r.event == "Weekday working hours" and r.workload.endswith("checkout-web")]
    assert weekly and weekly[0].monthly_scheduled_usd < weekly[0].monthly_at_peak_usd
    assert weekly[0].scheduled_savings_pct > 0
    assert 0 < weekly[0].window_fraction < 1


def test_dated_events_are_labelled_as_estimates(stats):
    rows = simulate(list(stats.values()), load_calendar(), multiplier=3.0, catalog=load_skus(),
                    event_name="Black Friday sale")
    assert rows and all("one week in four" in r.note for r in rows)


def test_unknown_event_matches_nothing(stats):
    assert simulate(list(stats.values()), load_calendar(), multiplier=3.0, catalog=load_skus(),
                    event_name="No such sale") == []


def test_no_calendar_falls_back_to_history_peaks(stats):
    rows = simulate([stats["checkout-web"]], None, multiplier=2.0, catalog=load_skus())
    assert rows and rows[0].event.startswith("Recurring")
    assert rows[0].additional_replicas == max(0, rows[0].replicas_needed - rows[0].replicas_now)
