"""Stage 1.5 forecasting: history peaks + the operator's business calendar, priced in percent."""
from datetime import date

import pytest

from kubesage.config import BusinessCalendar, CalendarEvent, load_calendar
from kubesage.forecast import build_forecasts, forecast_workload
from kubesage.llm.schemas import Recommendation, ScalingPlan
from kubesage.recommend import costing, guardrails
from kubesage.summary import markdown


def test_calendar_file_loads_and_matches_workloads():
    cal = load_calendar()
    assert [e.name for e in cal.for_workload("checkout-web")] == [
        "Weekday working hours", "Black Friday sale", "Christmas shopping peak", "New Year sale"]
    assert cal.for_workload("report-worker") == []  # a nightly batch job has no sale calendar
    black_friday = next(e for e in cal.events if e.name == "Black Friday sale")
    assert not black_friday.weekly and black_friday.traffic_multiplier == 3.0


def test_calendar_event_needs_exactly_one_schedule():
    with pytest.raises(ValueError):
        CalendarEvent(name="bad")
    with pytest.raises(ValueError):
        CalendarEvent(name="bad", days="1-5", date="2026-11-27")


def test_history_peak_becomes_a_prewarm_window(stats):
    f = forecast_workload(stats["checkout-web"])  # no calendar: history only
    assert f.windows and f.windows[0].source == "history"
    top = f.windows[0]
    assert (top.days, top.start_time, top.prewarm_time) == ("1-5", "09:00", "08:45")
    assert top.cron_start == "45 8 * * 1-5" and top.cron_end == "0 11 * * 1-5"
    assert top.multiplier > 1 and f.replicas_needed >= 2 and f.offpeak_replicas < f.replicas_needed
    assert 0 < top.confidence <= 0.95


def test_calendar_sale_forecasts_load_metrics_cannot_see(stats):
    cal = BusinessCalendar(events=[CalendarEvent(
        name="Black Friday sale", kind="sale", date="2026-11-27", repeat_yearly=True,
        start_time="08:00", end_time="22:00", traffic_multiplier=3.0, prewarm_minutes=30,
        workloads=["checkout-web"], note="triple traffic")])
    f = forecast_workload(stats["checkout-web"], cal, today=date(2026, 11, 1))
    sale = next(w for w in f.windows if w.event == "Black Friday sale")
    assert sale.schedule.startswith("2026-11-27 08:00")
    assert sale.expected_total_cpu >= f.baseline_total_cpu * 3.0  # declared 3x, floored at the proven rush
    assert sale.multiplier >= 3.0
    assert sale.prewarm_time == "07:30"
    assert sale.cron_start is None  # dated: forecast-only, never a fake weekly cron
    assert f.summary.startswith("checkout-web: Black Friday sale") or "Black Friday sale" in f.summary


def test_weekly_working_hours_get_a_cron_window_and_scale_down(stats):
    f = forecast_workload(stats["checkout-web"], load_calendar())
    hours = next(w for w in f.windows if w.event == "Weekday working hours")
    assert hours.cron_start == "45 8 * * 1-5" and hours.cron_end == "0 17 * * 1-5"
    assert hours.replicas_needed > f.offpeak_replicas  # busy in hours, lean outside them


def test_prewarm_before_midnight_shifts_the_cron_day(stats):
    cal = BusinessCalendar(events=[CalendarEvent(
        name="Midnight drop", days="5", start_time="00:10", end_time="02:00",
        traffic_multiplier=2.0, prewarm_minutes=30, workloads=["checkout-web"])])
    f = forecast_workload(stats["checkout-web"], cal)
    drop = next(w for w in f.windows if w.event == "Midnight drop")
    assert drop.prewarm_time == "23:40" and drop.cron_start == "40 23 * * 4"  # Thursday warms Friday's drop


def test_guardrails_add_a_forgotten_calendar_prewarm(stats):
    s = stats["checkout-web"]
    forecast = forecast_workload(s, load_calendar())
    bare = Recommendation(workload=s.id, risk="low", diagnosis="x",
                          scaling=ScalingPlan(min_replicas=1, max_replicas=2, cpu_utilization_target=70))
    safe, notes = guardrails.apply(bare, s, forecast)
    starts = {w.start for w in safe.scaling.cron_windows}
    assert "45 8 * * 1-5" in starts  # working-hours pre-warm the model never proposed
    assert safe.scaling.max_replicas >= forecast.replicas_needed
    assert any("calendar pre-warm" in n for n in notes)


def test_guardrails_without_forecast_are_unchanged(stats):
    s = stats["checkout-web"]
    bare = Recommendation(workload=s.id, risk="low", diagnosis="x",
                          scaling=ScalingPlan(min_replicas=1, max_replicas=2, cpu_utilization_target=70))
    safe, _ = guardrails.apply(bare, s)
    assert safe.scaling.cron_windows == []


def test_savings_pct_math():
    assert costing.savings_pct(200, 150) == 25.0
    assert costing.savings_pct(200, 260) == -30.0  # an increase reports as a negative saving, honestly
    assert costing.savings_pct(0, 0) is None
    assert costing.price_improvement_pct(246.74, 280.32) == pytest.approx(12.0, abs=0.1)


def test_analysis_with_calendar_carries_forecasts_and_percents(result):
    # conftest analyzes without a calendar (history only); forecasts and percents must still be present.
    assert result.forecasts and {f.workload for f in result.forecasts} == {w.id for w in result.workloads}
    for w in result.workloads:
        assert w.monthly_savings_usd == pytest.approx(w.monthly_before_usd - w.monthly_after_usd, abs=0.02)
        if w.monthly_before_usd > 0:
            assert w.monthly_savings_pct == pytest.approx(
                (w.monthly_before_usd - w.monthly_after_usd) / w.monthly_before_usd * 100, abs=0.15)
    assert result.node_pool.monthly_savings_pct is not None
    text = markdown(result)
    assert "saves %" in text and "Forecast (history + declared calendar)" in text


def test_mock_recommendation_uses_the_calendar(stats):
    from kubesage.config import load_calendar
    from kubesage.llm import mock

    s = stats["catalog-api"]  # healthy by history: only the calendar gives it anything to do
    forecast = forecast_workload(s, load_calendar())
    rec = mock.recommend(s, mock.triage_one(s), forecast)
    assert rec.scaling is not None
    assert any("Black Friday" in f for f in rec.follow_ups)  # dated event: timed scale-up, not a weekly cron


def test_forecasts_are_deterministic(stats):
    cal = load_calendar()
    a = build_forecasts(list(stats.values()), cal, today=date(2026, 11, 1))
    b = build_forecasts(list(stats.values()), cal, today=date(2026, 11, 1))
    assert a == b
