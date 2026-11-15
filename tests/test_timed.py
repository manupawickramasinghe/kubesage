"""Dated calendar events become KEDA cron triggers on the workload's own ScaledObject; weekly windows stay in the plan."""
from datetime import date

import yaml

from kubesage.config import BusinessCalendar, CalendarEvent, load_calendar
from kubesage.forecast import forecast_workload
from kubesage.llm.schemas import CronWindow, Recommendation, ResourceSpec, ScalingPlan
from kubesage.recommend import render
from kubesage.recommend.timed import dated_triggers

TODAY = date(2026, 11, 1)


def triggers(stats, *events, today=TODAY, workload="checkout-web"):
    forecast = forecast_workload(stats[workload], BusinessCalendar(events=list(events)), today=today)
    return forecast, dated_triggers(forecast, today=today)


def event(**kw):
    return CalendarEvent(**{"name": "Sale", "start_time": "08:00", "end_time": "22:00", "traffic_multiplier": 3.0, **kw})


def test_black_friday_is_one_trigger_with_its_date_prewarm_and_cooldown(stats):
    forecast = forecast_workload(stats["checkout-web"], load_calendar(), today=TODAY)
    friday = [t for t in dated_triggers(forecast, today=TODAY) if t.event == "Black Friday sale"]
    assert len(friday) == 1
    t = friday[0]
    assert (t.start, t.end) == ("30 7 27 11 *", "0 23 27 11 *")  # 08:00 minus 30 min, 22:00 plus 60 min
    window = next(w for w in forecast.windows if w.event == "Black Friday sale")
    assert t.replicas == window.replicas_needed and not t.one_off and "(yearly)" in t.label


def test_a_range_of_days_opens_and_closes_every_day_and_splits_at_a_month_end(stats):
    _, week = triggers(stats, event(name="Christmas", start_date="12-20", end_date="12-26", start_time="09:00",
                                    end_time="21:00", prewarm_minutes=20, cooldown_minutes=30))
    assert [(t.start, t.end) for t in week] == [("40 8 20-26 12 *", "30 21 20-26 12 *")]
    _, new_year = triggers(stats, event(name="New Year", start_date="12-30", end_date="01-02", prewarm_minutes=0))
    assert [(t.start, t.end) for t in new_year] == [("0 8 30-31 12 *", "0 22 30-31 12 *"), ("0 8 1-2 1 *", "0 22 1-2 1 *")]
    assert len({t.label for t in new_year}) == 1 and "2026-12-30 to 2027-01-02" in new_year[0].label


def test_a_one_off_says_so_and_is_dropped_once_it_has_passed(stats):
    _, upcoming = triggers(stats, event(date="2026-11-27"))
    assert upcoming[0].one_off and "one-off" in upcoming[0].label
    assert triggers(stats, event(date="2026-11-27"), today=date(2026, 11, 28))[1] == []
    assert triggers(stats, event(date="2026-11-27"), today=date(2026, 11, 27))[1] != []  # the day itself still counts
    _, yearly = triggers(stats, event(date="2026-11-27", repeat_yearly=True), today=date(2026, 11, 28))
    assert yearly[0].start == "45 7 27 11 *" and "2027-11-27" in yearly[0].label


def test_the_window_stays_inside_its_day(stats):
    _, t = triggers(stats, event(date="2026-12-31", start_time="00:10", end_time="23:30", prewarm_minutes=30,
                                 cooldown_minutes=60))
    assert (t[0].start, t[0].end) == ("0 0 31 12 *", "59 23 31 12 *")


def test_weekly_windows_and_history_peaks_render_nothing(stats):
    assert triggers(stats, event(days="1-5"))[1] == []
    assert dated_triggers(forecast_workload(stats["report-worker"], load_calendar())) == []  # no calendar events at all
    assert dated_triggers(None) == []


def scaled_object(files, name="web"):
    return yaml.safe_load(files[f"{name}.keda.yaml"])


def test_dated_triggers_join_the_plans_scaled_object(stats):
    _, dated = triggers(stats, event(date="2026-11-27"))
    plan = ScalingPlan(min_replicas=1, max_replicas=4, cpu_utilization_target=70,
                       cron_windows=[CronWindow(start="45 8 * * 1-5", end="0 17 * * 1-5", desired_replicas=3)])
    rec = Recommendation(workload="demo/web", diagnosis="x", risk="low", scaling=plan)
    so = scaled_object(render.render(rec, namespace="demo", name="web", container="web", tz="Asia/Colombo", dated=dated))
    kinds = [t["type"] for t in so["spec"]["triggers"]]
    assert kinds == ["cron", "cron", "cpu"]
    assert so["spec"]["triggers"][1]["metadata"] == {"timezone": "Asia/Colombo", "start": dated[0].start, "end": dated[0].end,
                                                     "desiredReplicas": str(dated[0].replicas)}
    assert so["spec"]["maxReplicaCount"] == max(4, dated[0].replicas) and so["spec"]["minReplicaCount"] == 1
    assert "Sale" in so["metadata"]["annotations"][render.ANNOTATION]
    assert so["metadata"]["labels"] == render.MANAGED_BY  # Reset demo and Apply find it by this label


def test_a_fixed_replica_plan_becomes_a_scaled_object_that_only_moves_for_the_event(stats):
    _, dated = triggers(stats, event(date="2026-11-27"))
    fixed = Recommendation(workload="demo/web", diagnosis="x", risk="low", scaling=ScalingPlan(min_replicas=2, max_replicas=2),
                           resources=ResourceSpec(cpu_request_m=250, cpu_limit_m=None, mem_request_mi=256, mem_limit_mi=512))
    files = render.render(fixed, namespace="demo", name="web", container="web", tz="UTC", dated=dated)
    so = scaled_object(files)
    assert so["spec"]["minReplicaCount"] == 2 and so["spec"]["maxReplicaCount"] == max(2, dated[0].replicas)
    assert [t["type"] for t in so["spec"]["triggers"]] == ["cron"]
    assert "replicas" not in yaml.safe_load(files["web.patch.yaml"])["spec"]  # the autoscaler owns the count now
    # Without a dated event, or with one that needs no more than the plan already runs, it stays a plain replica count.
    small = [d.model_copy(update={"replicas": 2}) for d in dated]
    for plain in (render.render(fixed, namespace="demo", name="web", container="web", tz="UTC"),
                  render.render(fixed, namespace="demo", name="web", container="web", tz="UTC", dated=small)):
        assert "web.keda.yaml" not in plain and yaml.safe_load(plain["web.patch.yaml"])["spec"]["replicas"] == 2


def test_a_recommendation_without_a_scaling_plan_keeps_the_current_replicas(stats):
    _, dated = triggers(stats, event(date="2026-11-27"))
    rec = Recommendation(workload="demo/web", diagnosis="x", risk="low",
                         resources=ResourceSpec(cpu_request_m=250, cpu_limit_m=None, mem_request_mi=256, mem_limit_mi=512))
    so = scaled_object(render.render(rec, namespace="demo", name="web", container="web", tz="UTC", dated=dated,
                                     current_replicas=3))
    assert so["spec"]["minReplicaCount"] == 3 and so["spec"]["maxReplicaCount"] == max(3, dated[0].replicas)
    assert render.render(rec, namespace="demo", name="web", container="web", tz="UTC", current_replicas=3).keys() == {"web.patch.yaml"}
