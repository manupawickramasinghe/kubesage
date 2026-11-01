"""Realized savings: measure after Apply what the analysis only predicted."""
from kubesage.config import load_skus
from kubesage.realized import realized_savings


def test_halved_requests_realize_about_half_the_cost(stats):
    before = list(stats.values())
    after = [s.model_copy(update={"cpu_request_cores": (s.cpu_request_cores or 0) / 2,
                                  "mem_request_mib": (s.mem_request_mib or 0) / 2}) for s in before]
    report = realized_savings(before, after, catalog=load_skus())
    assert report.realized_savings_pct > 40  # roughly half, in percent not just dollars
    assert report.realized_savings_usd > 0
    assert "Realized saving" in report.summary and "%" in report.summary


def test_prediction_gap_is_reported_in_points(stats):
    before = [stats["payment-service"]]
    after = [before[0].model_copy(update={"cpu_request_cores": 0.2, "mem_request_mib": 456})]
    report = realized_savings(before, after, catalog=load_skus(),
                              predicted_pct={"demo/payment-service": 88.2})
    row = report.rows[0]
    assert row.predicted_savings_pct == 88.2
    assert row.prediction_gap_pct is not None and abs(row.prediction_gap_pct) < 15


def test_missing_and_unchanged_workloads(stats):
    report = realized_savings(list(stats.values()), [stats["checkout-web"]], catalog=load_skus())
    assert [r.workload for r in report.rows] == ["demo/checkout-web"]  # missing peers skipped, not zeroed
    same = realized_savings([stats["catalog-api"]], [stats["catalog-api"]], catalog=load_skus())
    assert same.rows[0].realized_savings_pct == 0.0
