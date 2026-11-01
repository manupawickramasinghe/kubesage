"""SLO guardrails: a breached latency/error budget forbids scale-down, whatever the model proposed."""
from kubesage.llm.schemas import CronWindow, Recommendation, ScalingPlan
from kubesage.recommend import guardrails
from kubesage.recommend.guardrails import SLOSnapshot


def _rec(min_replicas: int, max_replicas: int) -> Recommendation:
    return Recommendation(workload="x", risk="low", diagnosis="cut replicas to save money",
                          scaling=ScalingPlan(min_replicas=min_replicas, max_replicas=max_replicas,
                                              cron_windows=[CronWindow(start="50 1 * * *", end="0 3 * * *",
                                                                       desired_replicas=max(max_replicas, 1))]))


def test_breached_latency_forbids_scale_down(stats):
    s = stats["checkout-web"]
    safe, notes = guardrails.apply(_rec(1, 3), s, slo=SLOSnapshot(latency_p99_ms=1_450.0))
    assert safe.scaling.min_replicas == s.replicas
    assert safe.scaling.max_replicas >= s.replicas
    assert any("SLO breach" in n and "no scale-down" in n for n in notes)
    assert "1,450ms" in " ".join(notes) or "1450ms" in " ".join(notes)
    assert safe.risk == "medium"


def test_breached_error_rate_forbids_scale_to_zero(stats):
    s = stats["report-worker"]  # a batch workload that may normally scale to zero
    safe, notes = guardrails.apply(_rec(0, 2), s, slo=SLOSnapshot(error_rate_pct=6.5))
    assert safe.scaling.min_replicas == max(s.replicas, 1)
    assert any("SLO breach" in n for n in notes)


def test_healthy_slo_changes_nothing(stats):
    s = stats["report-worker"]
    healthy = SLOSnapshot(latency_p99_ms=120.0, error_rate_pct=0.1)
    assert healthy.breached is None
    safe, notes = guardrails.apply(_rec(0, 2), s, slo=healthy)
    assert safe.scaling.min_replicas == 0
    assert not any("SLO" in n for n in notes)


def test_no_slo_keeps_existing_behaviour(stats):
    s = stats["report-worker"]
    safe, _ = guardrails.apply(_rec(0, 2), s)
    assert safe.scaling.min_replicas == 0


def test_breach_detection_thresholds():
    assert SLOSnapshot(latency_p99_ms=500.0).breached is None  # at budget is not over budget
    assert SLOSnapshot(latency_p99_ms=501.0).breached is not None
    assert SLOSnapshot(error_rate_pct=1.0).breached is None
    assert SLOSnapshot(error_rate_pct=1.1).breached is not None
    assert SLOSnapshot().breached is None  # no observations, no opinion
