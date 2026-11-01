"""Timed scale-up for dated calendar events: the part a weekly KEDA cron cannot express.

A Black Friday on 2026-11-27 is not "every Friday". For those forecast windows KubeSage renders a pair
of one-date Kubernetes CronJobs (their schedule carries day-of-month and month, which the KEDA cron
subset deliberately does not): one scales the Deployment up at the pre-warm time, one scales it back
down after the window plus cooldown. The jobs only run `kubectl scale`, they carry the event name in
their labels, and their schedule repeats yearly only when the calendar event says `repeat_yearly`; the
rendered YAML says so plainly so nobody mistakes a one-off for a weekly trigger.
"""
from __future__ import annotations

from datetime import date

from kubesage.forecast import WorkloadForecast

IMAGE = "bitnami/kubectl:latest"  # operators pin this in production; the manifest names it in one place


def _date_parts(window_schedule: str, today: date) -> tuple[int, int, int]:
    """(day, month, year) for a dated window. Range events (Dec 20-26) use their start date."""
    head = window_schedule.split(" ")[0]
    if "-" in head and len(head) == 5:  # "MM-DD" yearly range
        month, day = (int(p) for p in head.split("-"))
        year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
        return day, month, year
    when = date.fromisoformat(head)
    return when.day, when.month, when.year


def _job(name: str, namespace: str, deployment: str, replicas: int, schedule: str, event: str) -> dict:
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"kubesage.io/event": event.lower().replace(" ", "-")[:40],
                                "kubesage.io/managed-by": "kubesage"}},
        "spec": {"schedule": schedule, "jobTemplate": {"spec": {"template": {"spec": {
            "restartPolicy": "Never",
            "containers": [{"name": "scale", "image": IMAGE,
                            "command": ["kubectl", "scale", f"deployment/{deployment}",
                                        f"--replicas={replicas}", "-n", namespace]}]}}}}},
    }


def timed_manifests(forecast: WorkloadForecast, *, namespace: str, deployment: str,
                    today: date | None = None) -> dict[str, str]:
    """filename -> YAML for every dated (non-weekly) forecast window. Weekly windows return nothing:
    KEDA already owns them. One-off schedules repeat yearly in cron terms; the header says to delete
    the CronJob after the event for non-yearly events."""
    import yaml

    today = today or date.today()
    out: dict[str, str] = {}
    for window in forecast.windows:
        if window.source != "calendar" or window.cron_start is not None:
            continue
        day, month, _year = _date_parts(window.schedule, today)
        pre_h, pre_m = (int(p) for p in window.prewarm_time.split(":"))
        end_h, end_m = (int(p) for p in window.end_time.split(":"))
        slug = window.event.lower().replace(" ", "-")[:30]
        up = _job(f"kubesage-{deployment}-{slug}-up"[:63], namespace, deployment, window.replicas_needed,
                  f"{pre_m} {pre_h} {day} {month} *", window.event)
        down = _job(f"kubesage-{deployment}-{slug}-down"[:63], namespace, deployment,
                    forecast.offpeak_replicas, f"{end_m} {end_h} {day} {month} *", window.event)
        header = (f"# KubeSage timed event: {window.event} ({window.schedule})\n"
                  f"# Scales {deployment} to {window.replicas_needed} at {window.prewarm_time} "
                  f"(pre-warm) and back to {forecast.offpeak_replicas} at {window.end_time}.\n"
                  f"# Cron repeats yearly: for a one-off event, delete these CronJobs after it passes.\n")
        out[f"{deployment}.{slug}.timed.yaml"] = header + yaml.safe_dump(up, sort_keys=False) + "---\n" + yaml.safe_dump(down, sort_keys=False)
    return out
