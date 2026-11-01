"""Forecast verification: the agent grades its own predictions against later reality."""
from kubesage.config import load_calendar
from kubesage.forecast import forecast_workload
from kubesage.verify import verify, verify_all


def test_forecast_verified_against_its_own_history_is_close(stats):
    s = stats["checkout-web"]
    v = verify(forecast_workload(s, load_calendar()), s)
    assert v.verdict == "covered" and v.grade in "AB"
    assert v.accuracy > 0.8 and v.actual_peak_cpu > 0
    assert "forecast" in v.summary and "actual peak" in v.summary


def test_a_doubled_reality_is_an_under_forecast(stats):
    s = stats["checkout-web"]
    forecast = forecast_workload(s, load_calendar())
    doubled = s.model_copy(update={"cpu_total_p95": forecast.expected_peak_cpu * 2, "peaks": []})
    v = verify(forecast, doubled)
    assert v.verdict == "under-forecast" and v.error_pct > 40 and v.grade in "CD"


def test_no_observed_load_cannot_be_verified(stats):
    s = stats["catalog-api"]
    idle = s.model_copy(update={"cpu_total_p95": 0.0, "peaks": []})
    v = verify(forecast_workload(s), idle)
    assert v.error_pct is None and v.grade == "D"


def test_verify_all_skips_missing_workloads(stats):
    forecasts = [forecast_workload(s) for s in stats.values()]
    out = verify_all(forecasts, [stats["checkout-web"]])
    assert [v.workload for v in out] == ["demo/checkout-web"]
