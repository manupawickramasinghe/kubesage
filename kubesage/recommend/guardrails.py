"""Deterministic safety floors applied to every model recommendation before anything is rendered.

The model is allowed to be clever; it is not allowed to cut below what the workload was observed using.
Each clamp is recorded so the UI can show exactly where the guardrails overrode the model.
"""
from __future__ import annotations

import math
import statistics

from kubesage.llm.schemas import Recommendation, ScalingPlan
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import cron

MAX_REPLICAS = 20
DEFAULT_CPU_TARGET = 70
PEAK_SIGNALS = {"recurring-peak", "cpu-over-request"}  # workloads whose load outgrows their pods


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
                cron.active_minutes(w.start, w.end)
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
        if plan.min_replicas > MAX_REPLICAS:
            notes.append(f"min replicas capped {plan.min_replicas} -> {MAX_REPLICAS}")
            plan.min_replicas = MAX_REPLICAS
        if plan.min_replicas > 0 and PEAK_SIGNALS & set(s.signals):
            # Found in a live run: checkout-web got cron-only scaling. Cron windows cover the peaks seen so far;
            # without a CPU trigger an unplanned spike just saturates the pods.
            if plan.cpu_utilization_target is None:
                notes.append(f"added a CPU trigger at {DEFAULT_CPU_TARGET}% (cron-only scaling misses unplanned peaks)")
                plan.cpu_utilization_target = DEFAULT_CPU_TARGET
            if plan.max_replicas <= plan.min_replicas < MAX_REPLICAS:
                ceiling = min(plan.min_replicas * 2, MAX_REPLICAS)
                notes.append(f"max replicas raised {plan.max_replicas} -> {ceiling} (CPU scaling needs room to grow)")
                plan.max_replicas = ceiling
        if plan.min_replicas == 0 and plan.cpu_utilization_target is not None:
            notes.append("dropped CPU target: KEDA cannot scale to zero on CPU")
            plan.cpu_utilization_target = None
        if plan.min_replicas == 0 and not plan.cron_windows:
            notes.append("min replicas raised 0 -> 1 (no trigger left to wake the workload)")
            plan.min_replicas = 1
        if plan.max_replicas > MAX_REPLICAS:
            notes.append(f"max replicas capped {plan.max_replicas} -> {MAX_REPLICAS}")
            plan.max_replicas = MAX_REPLICAS
        _bounds(plan, notes)

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
    _aggregate(rec, s, notes)
    return rec, notes


def _bounds(plan: ScalingPlan, notes: list[str]) -> None:
    """Keep max >= min, every window between them and the CPU target in 30-95%, saying so whenever it moves."""
    if plan.max_replicas < max(plan.min_replicas, 1):
        notes.append(f"max replicas raised {plan.max_replicas} -> {max(plan.min_replicas, 1)} (must be >= min replicas)")
        plan.max_replicas = max(plan.min_replicas, 1)
    for w in plan.cron_windows:
        fixed = min(max(w.desired_replicas, plan.min_replicas, 1), plan.max_replicas)
        if fixed != w.desired_replicas:
            notes.append(f"cron window {w.start} replicas {w.desired_replicas} -> {fixed} (kept between min and max)")
            w.desired_replicas = fixed
    if plan.cpu_utilization_target is not None:
        fixed = min(max(plan.cpu_utilization_target, 30), 95)
        if fixed != plan.cpu_utilization_target:
            notes.append(f"CPU target {plan.cpu_utilization_target}% -> {fixed}% (kept between 30% and 95%)")
            plan.cpu_utilization_target = fixed


def _aggregate(rec: Recommendation, s: WorkloadStats, notes: list[str]) -> None:
    """Per-pod floors don't protect a replica cut: four pods using 0.8 cores in total can't become one pod capped
    at 0.25. The replicas the plan can reach, times what each pod may use, must cover the measured total."""
    r, plan = rec.resources, rec.scaling
    per_pod = ((r.cpu_limit_m or r.cpu_request_m) / 1000 if r else s.cpu_limit_cores or s.cpu_request_cores) or 0
    need = s.cpu_total_p95
    if per_pod <= 0 or need <= 0:
        return
    pods = min(MAX_REPLICAS, max(1, math.ceil(need / per_pod - 1e-9)))
    if plan is None:
        if r is not None and r.cpu_limit_m is not None and s.replicas * per_pod < need:
            limit = _up(need / max(s.replicas, 1) * 1000, 5)
            notes.append(f"CPU limit raised {r.cpu_limit_m}m -> {limit}m ({s.replicas} replicas must cover the "
                         f"{need:.2f}-core p95 total)")
            r.cpu_limit_m = limit
        return
    reach = max([plan.max_replicas if plan.cpu_utilization_target is not None else plan.min_replicas]
                + [w.desired_replicas for w in plan.cron_windows])
    if reach * per_pod < need:
        why = f"{reach} x {per_pod:g} cores can't cover the {need:.2f}-core p95 total across pods"
        if plan.cpu_utilization_target is None and plan.min_replicas > 0:
            notes.append(f"added a CPU trigger at {DEFAULT_CPU_TARGET}% ({why})")
            plan.cpu_utilization_target = DEFAULT_CPU_TARGET
        if plan.cpu_utilization_target is not None:
            notes.append(f"max replicas raised {plan.max_replicas} -> {pods} ({why})")
            plan.max_replicas = max(plan.max_replicas, pods)
        else:  # scale-to-zero on cron only: the windows are when the load runs
            plan.max_replicas = max(plan.max_replicas, pods)
            for w in plan.cron_windows:
                if w.desired_replicas < pods:
                    notes.append(f"cron window {w.start} replicas {w.desired_replicas} -> {pods} ({why})")
                    w.desired_replicas = pods
    if plan.min_replicas > 0:
        typical = float(statistics.median(s.weekday_hourly_cpu + s.weekend_hourly_cpu)) if s.weekday_hourly_cpu else 0.0
        floor = min(MAX_REPLICAS, math.ceil(typical / per_pod - 1e-9))
        if plan.min_replicas < floor:
            notes.append(f"min replicas raised {plan.min_replicas} -> {floor} (median total load {typical:.2f} cores "
                         f"needs {floor} pods of {per_pod:g} cores)")
            plan.min_replicas = floor
            _bounds(plan, notes)
