"""Unit economics: dollars per 1k units of business, from operator-supplied traffic only."""
import pytest

from kubesage.unit_economics import cost_per_1k_requests, requests_per_month, unit_economics


def test_requests_per_month_uses_the_costing_month():
    assert requests_per_month(1.0) == pytest.approx(730 * 3600)
    assert requests_per_month(0.0) == 0.0
    with pytest.raises(ValueError):
        requests_per_month(-1.0)


def test_cost_per_1k_requests():
    assert cost_per_1k_requests(100.0, 1_000_000) == pytest.approx(0.1)
    assert cost_per_1k_requests(0.0, 1_000_000) == 0.0
    assert cost_per_1k_requests(100.0, 0) is None  # no traffic: no invented unit cost


def test_unit_economics_before_and_after():
    u = unit_economics("demo/checkout-web", 38.85, avg_rps=12.0, monthly_after_usd=34.30, unit="checkout")
    assert u.monthly_units == pytest.approx(12.0 * 730 * 3600)
    assert u.cost_per_1k_after_usd < u.cost_per_1k_usd
    assert u.unit_savings_pct == pytest.approx(11.7, abs=0.1)
    assert "per 1k checkouts" in u.summary and "cheaper" in u.summary


def test_measured_monthly_units_win_over_rps_estimate():
    u = unit_economics("demo/api", 50.0, avg_rps=999.0, monthly_units=2_000_000)
    assert u.monthly_units == 2_000_000
    assert u.cost_per_1k_usd == pytest.approx(0.025)


def test_no_traffic_is_reported_not_invented():
    u = unit_economics("demo/quiet-worker", 5.0)
    assert u.cost_per_1k_usd is None and u.unit_savings_pct is None
    assert "no traffic figure" in u.summary
