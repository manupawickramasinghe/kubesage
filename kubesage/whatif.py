"""What-if simulation: "what happens if the sale is 5x, not 3x?" — answered deterministically, $0, no LLM.

A judge (or an operator before a real sale) can vary the traffic multiplier of a declared calendar event
or a recurring peak and immediately see the replica count, the pre-warm time and the money: the cost if
the cluster sat at peak size all month versus scheduling the peak only for the window. This is the
scheduled-vs-always-on comparison that makes pre-warming's saving explicit instead of asserted.
"""
from __future__ import annotations

import math

from pydantic import BaseModel

from kubesage.config import BusinessCalendar, SkuCatalog
from kubesage.forecast import _baseline, _per_pod_capacity, _replicas
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import costing, cron


class Scenario(BaseModel):
    workload: str
    event: str
    multiplier: float
    baseline_total_cpu: float
    expected_total_cpu: float
    replicas_now: int
    replicas_needed: int
    additional_replicas: int
    monthly_now_usd: float        # allocation cost at today's replica count, all month
    monthly_at_peak_usd: float    # allocation cost if peak size were held all month (the waste)
    monthly_scheduled_usd: float  # allocation cost when peak size is held only for the window
    scheduled_savings_pct: float | None  # scheduled vs always-on-peak; positive = scheduling saves
    window_fraction: float        # share of the week the window is active (1.0 for dated events: see note)
    note: str = ""


def _weekly_fraction(start_cron: str, end_cron: str) -> float:
    return len(cron.active_minutes(start_cron, end_cron)) / cron.MINUTES_PER_WEEK


def simulate(stats: list[WorkloadStats], calendar: BusinessCalendar | None = None, *,
             multiplier: float, catalog: SkuCatalog, event_name: str | None = None) -> list[Scenario]:
    """Price one what-if multiplier for every workload the calendar (or its peaks) touches.

    `monthly_scheduled_usd` blends the off-peak replica count outside the window with the peak count
    inside it. For dated events the window fraction is unknown on a weekly axis, so they are priced as
    one week in four (fraction 0.25) and the note says so — an estimate, labelled as one.
    """
    calendar = calendar or BusinessCalendar()
    out: list[Scenario] = []
    for s in stats:
        baseline = _baseline(s)
        capacity = _per_pod_capacity(s)
        cpu_req, mem_req = s.cpu_request_cores or 0.0, s.mem_request_mib or 0.0

        windows: list[tuple[str, float, bool]] = []  # (event, active fraction of time, is_dated)
        for event in calendar.for_workload(s.name):
            if event_name and event.name != event_name:
                continue
            if event.weekly and event.days:
                from kubesage.forecast import _cron_at, _parse_hhmm
                pre = event.prewarm_minutes if event.prewarm_minutes is not None else calendar.prewarm_minutes
                windows.append((event.name, _weekly_fraction(_cron_at(event.days, _parse_hhmm(event.start_time) - pre),
                                                             _cron_at(event.days, _parse_hhmm(event.end_time))), False))
            else:
                windows.append((event.name, 0.25, True))
        if not windows and s.peaks and not event_name:
            for peak in s.peaks:
                from kubesage.forecast import _cron_at
                windows.append((f"Recurring {peak.start_hour:02d}:00 peak",
                                _weekly_fraction(_cron_at(peak.days, peak.start_hour * 60 - 15),
                                                 _cron_at(peak.days, peak.end_hour * 60)), False))
        for name, fraction, dated in windows:
            expected = baseline * multiplier
            if multiplier > 1 and s.peaks:
                expected = max(expected, max(p.peak_total_cpu for p in s.peaks))
            needed = _replicas(expected, capacity)
            offpeak = _replicas(baseline, capacity)
            now = costing.allocation_monthly(cpu_req, mem_req, s.replicas, catalog)
            at_peak = costing.allocation_monthly(cpu_req, mem_req, needed, catalog)
            scheduled_replicas = offpeak * (1 - fraction) + needed * fraction
            scheduled = costing.allocation_monthly(cpu_req, mem_req, scheduled_replicas, catalog)
            out.append(Scenario(
                workload=s.id, event=name, multiplier=multiplier,
                baseline_total_cpu=round(baseline, 3), expected_total_cpu=round(expected, 3),
                replicas_now=s.replicas, replicas_needed=needed,
                additional_replicas=max(0, needed - s.replicas),
                monthly_now_usd=round(now, 2), monthly_at_peak_usd=round(at_peak, 2),
                monthly_scheduled_usd=round(scheduled, 2),
                scheduled_savings_pct=costing.savings_pct(at_peak, scheduled),
                window_fraction=round(fraction, 3),
                note=("Dated event priced as one week in four; exact duration changes the scheduled cost."
                      if dated else
                      "Scheduled = off-peak replicas outside the window, peak replicas inside it.")))
    return out
