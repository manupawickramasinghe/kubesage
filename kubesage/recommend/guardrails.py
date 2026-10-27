"""Deterministic safety floors applied to every model recommendation before anything is rendered.

The model is allowed to be clever; it is not allowed to cut below what the workload was observed using.
Each clamp is recorded so the UI can show exactly where the guardrails overrode the model.
"""
from __future__ import annotations

import math
import statistics

from kubesage.llm.schemas import Recommendation
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import cron

MAX_REPLICAS = 20


def _up(value: float, multiple: int) -> int:
    return int(math.ceil(value / multiple) * multiple)


def _floor(value: int, floor: int, label: str, unit: str, why: str, notes: list[str]) -> int:
    if value < floor:
        notes.append(f"{label} raised {value}{unit} -> {floor}{unit} ({why})")
        return floor
    return value


def apply(rec: Recommendation, s: WorkloadStats) -> tuple[Recommendation, list[str]]:
    rec = rec.model_copy(deep=True)
    rec.workload = s.id
    notes: list[str] = []

    plan = rec.scaling
    if plan is not None:
        valid = []
        for w in plan.cron_windows:
            try:
                cron.parse(w.start), cron.parse(w.end)
                valid.append(w)
            except ValueError as exc:
                notes.append(f"dropped cron window: {exc}")
        plan.cron_windows = valid
        if plan.min_replicas == 0 and "batch-pattern" not in s.signals:
            # Scale-to-zero is for workloads that are genuinely idle between runs. Anything with steady
            # traffic (a web frontend, an API) would be offline outside its cron windows.
            per_pod = max(rec.resources.cpu_request_m / 1000 if rec.resources else s.cpu_request_cores or 0,
                          s.cpu_pod_p95, 0.01)  # a reckless tiny request must not inflate the replica count
            typical = float(statistics.median(s.weekday_hourly_cpu + s.weekend_hourly_cpu))
            floor = max(1, math.ceil(typical / per_pod))
            notes.append(f"min replicas raised 0 -> {floor} (not a batch workload: median load {typical:.2f} cores "
                         "never goes idle)")
            plan.min_replicas = floor
        if plan.min_replicas == 0 and plan.cpu_utilization_target is not None:
            notes.append("dropped CPU target: KEDA cannot scale to zero on CPU")
            plan.cpu_utilization_target = None
        if plan.min_replicas == 0 and not plan.cron_windows:
            notes.append("min replicas raised 0 -> 1 (no trigger left to wake the workload)")
            plan.min_replicas = 1
        if plan.max_replicas > MAX_REPLICAS:
            notes.append(f"max replicas capped {plan.max_replicas} -> {MAX_REPLICAS}")
            plan.max_replicas = MAX_REPLICAS
        plan.max_replicas = max(plan.max_replicas, plan.min_replicas, 1)
        for w in plan.cron_windows:
            w.desired_replicas = min(max(w.desired_replicas, plan.min_replicas, 1), plan.max_replicas)
        if plan.cpu_utilization_target is not None:
            plan.cpu_utilization_target = min(max(plan.cpu_utilization_target, 30), 95)

    r = rec.resources
    if r is not None:
        scale_to_zero = plan is not None and plan.min_replicas == 0
        cpu_typical = s.cpu_active_p50 if scale_to_zero else s.cpu_pod_p50
        mem_typical = s.mem_active_p95_mib if scale_to_zero else s.mem_pod_p50_mib
        why_cpu = "active-period median" if scale_to_zero else "30-day median usage"
        r.cpu_request_m = _floor(r.cpu_request_m, max(10, _up(cpu_typical * 1000, 5)), "CPU request", "m", why_cpu, notes)
        r.mem_request_mi = _floor(r.mem_request_mi, _up(mem_typical, 8), "memory request", "Mi",
                                  "active-period p95" if scale_to_zero else "30-day median usage", notes)
        r.mem_limit_mi = _floor(r.mem_limit_mi, _up(s.mem_pod_max_mib * 1.15, 8), "memory limit", "Mi",
                                "1.15x observed max", notes)
        r.mem_limit_mi = _floor(r.mem_limit_mi, r.mem_request_mi, "memory limit", "Mi", "must be >= request", notes)
        if r.cpu_limit_m is not None:
            r.cpu_limit_m = _floor(r.cpu_limit_m, max(r.cpu_request_m, _up(s.cpu_pod_p99 * 1000, 5)),
                                   "CPU limit", "m", "p99 usage / request", notes)
    return rec, notes
