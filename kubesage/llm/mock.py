"""Deterministic stand-ins for each model tier, used in mock mode and as a fallback for gaps in model output.

They follow the same rules the prompts give the real models, so demos without an API key still produce
sensible, explainable recommendations.
"""
from __future__ import annotations

import math

from kubesage.llm.schemas import (CronWindow, Recommendation, ResourceSpec, ScalingPlan, SkuChoice, TriageItem,
                                  TriageReport)
from kubesage.metrics.stats import PeakWindow, WorkloadStats
from kubesage.recommend.costing import SkuCandidate

COMPLEX_SIGNALS = {"oom-restarts", "restarts", "memory-growth"}
DAYS = {"*": "daily", "1-5": "weekdays", "0,6": "weekends"}


def triage_one(s: WorkloadStats) -> TriageItem:
    sig = set(s.signals)
    if sig & COMPLEX_SIGNALS:
        status, summary = "COMPLEX", "Restarts and/or memory growth need root-cause analysis before resizing."
    elif sig:
        status, summary = "ROUTINE", "Standard right-sizing or scaling opportunity."
    else:
        status, summary = "HEALTHY", "Requests match observed usage."
    return TriageItem(workload=s.id, status=status, signals=s.signals, summary=summary)


def triage(stats: list[WorkloadStats]) -> TriageReport:
    return TriageReport(items=[triage_one(s) for s in stats])


def _m(cores: float) -> int:
    return max(10, int(math.ceil(cores * 1000 / 5) * 5))


def _mi(mib: float) -> int:
    return int(math.ceil(mib / 8) * 8)


def _window(p: PeakWindow, desired: int, prewarm_min: int = 10) -> CronWindow:
    if p.start_hour == 0:
        start = f"0 0 * * {p.days}"
    else:
        start = f"{60 - prewarm_min} {p.start_hour - 1} * * {p.days}"
    return CronWindow(start=start, end=f"0 {p.end_hour % 24} * * {p.days}", desired_replicas=desired)


def _offpeak_total(s: WorkloadStats) -> float:
    hot = {h for p in s.peaks for h in range(p.start_hour, p.end_hour)}
    values = [v for prof in (s.weekday_hourly_cpu, s.weekend_hourly_cpu) for h, v in enumerate(prof) if h not in hot]
    return max(values, default=s.cpu_total_p95)


def recommend(s: WorkloadStats, item: TriageItem) -> Recommendation:
    sig = set(s.signals)
    cpu_req_now = s.cpu_request_cores or s.cpu_pod_p95
    days = s.window_days
    scaling = None
    follow_ups: list[str] = []

    if "batch-pattern" in sig and s.peaks:
        p = s.peaks[0]
        cpu_req, mem_req = s.cpu_active_p95 * 1.1, s.mem_active_p95_mib * 1.1
        scaling = ScalingPlan(min_replicas=0, max_replicas=max(1, s.replicas),
                              cron_windows=[_window(pk, max(1, s.replicas)) for pk in s.peaks])
        risk = "low"
        diagnosis = (f"Busy only {s.active_fraction:.0%} of the time: a {DAYS[p.days]} job around "
                     f"{p.start_hour:02d}:00-{p.end_hour:02d}:00 uses ~{s.cpu_active_p95:.2f} cores, and the pod idles "
                     f"at {s.cpu_pod_p50:.3f} cores otherwise while holding {cpu_req_now:.2f} cores 24/7. Scale to zero "
                     f"outside the window with a KEDA cron trigger that pre-warms 10 minutes early.")
    elif s.peaks:
        p = s.peaks[0]
        cpu_req, mem_req = cpu_req_now, s.mem_pod_p95_mib * 1.2
        per_pod = cpu_req * 0.7
        offpeak = _offpeak_total(s)
        base = max(2, math.ceil(offpeak / per_pod))
        windows = [_window(pk, max(base, math.ceil(pk.peak_total_cpu / per_pod))) for pk in s.peaks]
        top = max(w.desired_replicas for w in windows)
        scaling = ScalingPlan(min_replicas=base, max_replicas=top + 2, cron_windows=windows, cpu_utilization_target=70)
        risk = "medium"
        diagnosis = (f"Pods run above their CPU request {s.cpu_over_request_pct:.1f}% of the time, concentrated in a "
                     f"{DAYS[p.days]} {p.start_hour:02d}:00-{p.end_hour:02d}:00 peak of ~{p.peak_total_cpu:.2f} cores total "
                     f"vs ~{offpeak:.2f} off-peak. The pod shape is fine; the replica count is static. Run {base} replicas "
                     f"off-peak, pre-scale to {top} ten minutes before the peak, and let a 70% CPU target catch the rest.")
    else:
        cpu_req, mem_req = s.cpu_pod_p95 * 1.2, s.mem_pod_p95_mib * 1.2
        risk = "low"
        diagnosis = (f"CPU p95 is {s.cpu_pod_p95:.2f} cores against a {cpu_req_now:.2f}-core request and memory p99 is "
                     f"{s.mem_pod_p99_mib:.0f} MiB of {s.mem_request_mib or 0:.0f} MiB requested, flat across {days:.0f} days "
                     f"with no recurring peaks. Drop requests to p95 plus 20% headroom.")

    mem_lim = max(s.mem_pod_max_mib * 1.25, mem_req)
    if sig & {"memory-growth", "oom-restarts"}:
        mem_req = s.mem_pod_p95_mib
        scaling = ScalingPlan(min_replicas=max(2, s.replicas), max_replicas=max(3, s.replicas + 1),
                              cpu_utilization_target=75)
        risk = "high"
        old_lim = s.mem_limit_mib or s.mem_pod_max_mib
        extra_days = (_mi(mem_lim) - old_lim) / s.mem_growth_mib_per_day if s.mem_growth_mib_per_day > 0 else 0
        diagnosis = (f"Memory grows ~{s.mem_growth_mib_per_day:.0f} MiB/day and the container restarted {s.restarts}x in "
                     f"{days:.0f} days after reaching its {old_lim:.0f} MiB limit: OOM kills from a leak, not a "
                     f"steady-state plateau, so cutting memory would make it worse. Raising the limit to {_mi(mem_lim)} MiB "
                     f"adds ~{extra_days:.1f} days between OOM kills as a stopgap; 2 replicas keep traffic flowing "
                     f"through restarts.")
        follow_ups = ["Heap-profile the service to find the unbounded structure (cache, session map, listeners).",
                      "Alert on working-set growth > 50 MiB/day so the next leak is caught before the OOM."]

    cpu_lim = None if s.cpu_limit_cores is None else max(cpu_req * 1.25, s.cpu_pod_max * 1.2)
    if cpu_lim and risk == "high":
        cpu_lim = max(cpu_lim, s.cpu_limit_cores)  # don't tighten CPU on a workload already in trouble
    resources = ResourceSpec(cpu_request_m=_m(cpu_req), cpu_limit_m=_m(cpu_lim) if cpu_lim else None,
                             mem_request_mi=_mi(mem_req), mem_limit_mi=_mi(mem_lim))
    return Recommendation(workload=s.id, risk=risk, diagnosis=diagnosis, resources=resources, scaling=scaling,
                          follow_ups=follow_ups)


def sku_choice(candidates: list[SkuCandidate], sustained_load: bool) -> SkuChoice:
    pool = [c for c in candidates if not (sustained_load and c.family == "burstable")] or candidates
    best = pool[0]
    why = "cheapest candidate that fits"
    if sustained_load and pool is not candidates and candidates[0].family == "burstable":
        why += f"; skipped burstable {candidates[0].sku} because batch runs/recurring peaks would drain CPU credits"
    return SkuChoice(sku=best.sku, nodes=best.nodes, rationale=f"{best.nodes}x {best.sku} at ${best.monthly_usd:,.0f}/mo: {why}.")
