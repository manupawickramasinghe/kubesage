"""End-to-end in mock mode: scenario curves -> Stage 1 stats -> routing -> guardrails -> manifests."""
import pytest
import yaml

from kubesage.llm.schemas import CronWindow, Recommendation, ResourceSpec, ScalingPlan
from kubesage.recommend import costing, cron, guardrails


def test_stage1_detects_each_anti_pattern(stats):
    assert stats["catalog-api"].signals == []
    assert {"cpu-overprovisioned", "mem-overprovisioned"} <= set(stats["payment-service"].signals)
    assert "batch-pattern" in stats["report-worker"].signals
    batch_peak = stats["report-worker"].peaks[0]
    assert (batch_peak.days, batch_peak.start_hour, batch_peak.end_hour) == ("*", 2, 3)
    rush = stats["checkout-web"].peaks[0]
    assert (rush.days, rush.start_hour, rush.end_hour) == ("1-5", 9, 11)
    auth = stats["auth-api"]
    assert {"memory-growth", "oom-restarts"} <= set(auth.signals)
    assert auth.restarts >= 3
    assert 90 < auth.mem_growth_mib_per_day < 140


def test_routing(result):
    route = {w.id.split("/")[1]: (w.triage.status, w.tier) for w in result.workloads}
    assert route["catalog-api"] == ("HEALTHY", None)
    assert route["payment-service"] == ("ROUTINE", "worker")
    assert route["report-worker"] == ("ROUTINE", "worker")
    assert route["checkout-web"] == ("ROUTINE", "worker")
    assert route["auth-api"] == ("COMPLEX", "strategist")
    assert [e.stage for e in result.ledger].count("recommend") == 4  # healthy workload costs no stage-3 tokens


def test_recommendations_and_manifests(result):
    by = {w.id.split("/")[1]: w for w in result.workloads}
    report = by["report-worker"].recommendation
    assert report.scaling.min_replicas == 0 and report.scaling.cron_windows[0].start == "50 1 * * *"
    assert by["payment-service"].monthly_after_usd < by["payment-service"].monthly_before_usd / 3
    so = yaml.safe_load(by["checkout-web"].manifests["checkout-web.keda.yaml"])
    assert so["kind"] == "ScaledObject"
    assert {t["type"] for t in so["spec"]["triggers"]} == {"cron", "cpu"}
    patch = yaml.safe_load(by["payment-service"].manifests["payment-service.patch.yaml"])
    assert patch["spec"]["template"]["spec"]["containers"][0]["name"] == "payment-service"
    # A safe pool may cost more when the CPU autoscaler needs extra capacity; never promise savings
    # by ignoring its maximum replica count.
    assert result.node_pool.after_monthly_usd == min(
        c.monthly_usd for c in result.node_pool.candidates if c.family != "burstable")
    assert result.node_pool.choice.sku != "B4ms"  # sustained batch/peak load rules out burstable nodes
    assert len(result.node_pool.migrations) == len(result.workloads)  # every workload moves, healthy ones too
    migrate = yaml.safe_load(result.node_pool.migrations["catalog-api.migrate.yaml"])
    assert migrate["spec"]["template"]["spec"]["nodeSelector"] == {"kubesage.io/sku": result.node_pool.choice.sku}
    assert result.llm_cost_usd < result.baselines_usd["all_calls_on_strategist"]


def test_guardrails_raise_unsafe_model_output(stats):
    s = stats["payment-service"]
    reckless = Recommendation(workload="whatever", risk="low", diagnosis="cut everything",
                              resources=ResourceSpec(cpu_request_m=1, cpu_limit_m=1, mem_request_mi=16, mem_limit_mi=16),
                              scaling=ScalingPlan(min_replicas=0, max_replicas=500, cpu_utilization_target=50))
    rec, notes = guardrails.apply(reckless, s)
    assert rec.workload == s.id
    assert rec.resources.mem_limit_mi >= s.mem_pod_max_mib * 1.15
    assert rec.resources.cpu_request_m >= s.cpu_pod_p50 * 1000
    assert rec.scaling.min_replicas == 1  # not a batch workload, so no scale-to-zero
    assert rec.scaling.max_replicas == guardrails.MAX_REPLICAS
    assert len(notes) >= 5


def test_scale_to_zero_only_for_batch_workloads(stats):
    """Found in the first live run: the model scaled checkout-web (a web frontend) to zero off-peak."""
    def plan_for(name):
        rec = Recommendation(workload=name, risk="low", diagnosis="x", scaling=ScalingPlan(
            min_replicas=0, max_replicas=3, cron_windows=[CronWindow(start="50 8 * * 1-5", end="0 11 * * 1-5",
                                                                     desired_replicas=3)]))
        return guardrails.apply(rec, stats[name])

    web, notes = plan_for("checkout-web")
    assert web.scaling.min_replicas >= 1 and any("not a batch workload" in n for n in notes)
    batch, notes = plan_for("report-worker")
    assert batch.scaling.min_replicas == 0 and not notes


def test_peaky_workloads_keep_a_cpu_trigger(stats):
    """Found in a live run: cron-only scaling for checkout-web, so the traffic spike never added pods."""
    def plan_for(name):
        rec = Recommendation(workload=name, risk="low", diagnosis="x", scaling=ScalingPlan(
            min_replicas=3, max_replicas=3, cron_windows=[CronWindow(start="50 8 * * 1-5", end="0 11 * * 1-5",
                                                                     desired_replicas=3)]))
        return guardrails.apply(rec, stats[name])

    web, notes = plan_for("checkout-web")
    assert web.scaling.cpu_utilization_target == guardrails.DEFAULT_CPU_TARGET and web.scaling.max_replicas == 6
    assert any("CPU trigger" in n for n in notes)
    steady, _ = plan_for("catalog-api")
    assert steady.scaling.cpu_utilization_target is None


def test_cron_subset():
    assert cron.window_hours_per_week("50 8 * * 1-5", "0 11 * * 1-5") == pytest.approx(130 / 60 * 5)
    assert cron.window_hours_per_week("0 23 * * *", "0 1 * * *") == pytest.approx(14)
    with pytest.raises(ValueError):
        cron.parse("*/5 * * * *")


def test_cron_windows_include_end_days_and_weekend_carry():
    # A single Friday-to-Monday window is not a two-hour daily window.
    assert cron.window_hours_per_week("0 23 * * 5", "0 1 * * 1") == 50
    # The last weekday overnight start remains active until Monday's end event.
    assert cron.window_hours_per_week("0 23 * * 1-5", "0 1 * * 1-5") == 58
    active = cron.active_minutes("0 9 * * *", "0 11 * * *")
    assert 9 * 60 in active
    assert 11 * 60 not in active


@pytest.mark.parametrize("start,end", [("0 9 * * *", "0 9 * * *"), ("0 9 * * 0,6", "0 9 * * 6,0")])
def test_guardrails_drop_zero_length_cron_windows(stats, start, end):
    rec = Recommendation(workload="report-worker", risk="low", diagnosis="x", scaling=ScalingPlan(
        min_replicas=0, max_replicas=1,
        cron_windows=[CronWindow(start=start, end=end, desired_replicas=1)]))
    safe, notes = guardrails.apply(rec, stats["report-worker"])
    assert not safe.scaling.cron_windows
    assert safe.scaling.min_replicas == 1  # no valid wake-up trigger remains
    assert any("dropped cron window" in n for n in notes)


@pytest.mark.parametrize("day", ["7", "0-7", "1-"])
def test_cron_rejects_days_the_keda_parser_cannot_use(day):
    with pytest.raises(ValueError):
        cron.parse(f"0 9 * * {day}")


def test_overlapping_cron_costs_use_the_largest_target():
    plan = ScalingPlan(min_replicas=1, max_replicas=5, cron_windows=[
        CronWindow(start="0 9 * * *", end="0 12 * * *", desired_replicas=3),
        CronWindow(start="0 11 * * *", end="0 13 * * *", desired_replicas=5),
    ])
    # Each day: 20h at 1 replica, 2h at 3, 2h at 5. The overlapping hour is counted once.
    assert costing.average_replicas(plan, 1) == pytest.approx((20 + 2 * 3 + 2 * 5) / 24)
    plan.cron_windows.append(plan.cron_windows[0].model_copy())
    assert costing.average_replicas(plan, 1) == pytest.approx(1.5)


def test_node_capacity_includes_cpu_scaling_ceiling(result):
    expected_cpu = expected_mem = 0.0
    stats_by_id = {s.id: s for s in result.stats}
    for w in result.workloads:
        s = stats_by_id[w.id]
        rec = w.recommendation
        r = rec.resources if rec else None
        plan = rec.scaling if rec else None
        replicas = costing.peak_replicas(plan, s.replicas)
        if plan and plan.cpu_utilization_target is not None:
            assert replicas == plan.max_replicas
        expected_cpu += (r.cpu_request_m / 1000 if r else s.cpu_request_cores or 0) * replicas
        expected_mem += (r.mem_request_mi if r else s.mem_request_mib or 0) * replicas
    assert result.node_pool.demand_after.cpu_cores == pytest.approx(expected_cpu, abs=0.001)
    assert result.node_pool.demand_after.mem_mib == pytest.approx(expected_mem, abs=0.1)


def test_guardrails_cap_minimum_and_preserve_cpu_scaling_headroom(stats):
    huge = Recommendation(workload="checkout-web", risk="low", diagnosis="x", scaling=ScalingPlan(
        min_replicas=500, max_replicas=500, cpu_utilization_target=70))
    safe, notes = guardrails.apply(huge, stats["checkout-web"])
    assert safe.scaling.min_replicas == safe.scaling.max_replicas == guardrails.MAX_REPLICAS
    assert any("min replicas capped" in n for n in notes)
    # Existing CPU triggers need headroom too, not only triggers inserted by the guardrail.
    huge.scaling.min_replicas = huge.scaling.max_replicas = 3
    safe, notes = guardrails.apply(huge, stats["checkout-web"])
    assert safe.scaling.max_replicas > safe.scaling.min_replicas
    assert any("room to grow" in n for n in notes)
