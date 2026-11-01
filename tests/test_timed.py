"""Timed events: dated sales get real one-date scale-up manifests, weekly windows stay with KEDA."""
from datetime import date

import yaml

from kubesage.config import load_calendar
from kubesage.forecast import forecast_workload
from kubesage.recommend.timed import timed_manifests


def test_black_friday_renders_paired_cronjobs(stats):
    forecast = forecast_workload(stats["checkout-web"], load_calendar(), today=date(2026, 11, 1))
    files = timed_manifests(forecast, namespace="demo", deployment="checkout-web", today=date(2026, 11, 1))
    friday = next(text for name, text in files.items() if "black-friday" in name)
    docs = [d for d in yaml.safe_load_all(friday) if d]
    assert [d["kind"] for d in docs] == ["CronJob", "CronJob"]
    up, down = docs
    assert up["spec"]["schedule"] == "30 7 27 11 *"  # 07:30 pre-warm on 27 Nov
    assert down["spec"]["schedule"].endswith("27 11 *")
    up_args = up["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
    down_args = down["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
    assert f"--replicas={forecast.replicas_needed}" in up_args
    assert f"--replicas={forecast.offpeak_replicas}" in down_args
    assert "delete these CronJobs" in friday  # one-off honesty, in the file itself


def test_weekly_windows_render_nothing(stats):
    forecast = forecast_workload(stats["report-worker"], load_calendar())  # no calendar events at all
    assert timed_manifests(forecast, namespace="demo", deployment="report-worker") == {}
