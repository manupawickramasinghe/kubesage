"""Stage 1: deterministic usage statistics. Costs $0 and runs before any LLM sees the data.

Every number an LLM later reasons about is computed here, so models never spend tokens on arithmetic.
"""
from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from kubesage.timeutil import local_parts

MI = 1024**2


class PeakWindow(BaseModel):
    days: str                 # cron day-of-week field: "*", "1-5" or "0,6"
    start_hour: int           # local time, inclusive
    end_hour: int             # local time, exclusive
    peak_total_cpu: float     # p95 of summed pod CPU inside the window
    baseline_total_cpu: float  # median hourly CPU of the same days
    recurrence: float         # share of matching days on which the peak actually happened


class WorkloadStats(BaseModel):
    id: str
    namespace: str
    name: str
    window_days: float
    replicas: int
    cpu_request_cores: float | None
    cpu_limit_cores: float | None
    mem_request_mib: float | None
    mem_limit_mib: float | None
    # Per-pod CPU (max across pods at each instant) and total CPU (sum across pods), cores.
    cpu_pod_p50: float
    cpu_pod_p95: float
    cpu_pod_p99: float
    cpu_pod_max: float
    cpu_total_p95: float
    cpu_total_max: float
    cpu_over_request_pct: float
    # "Active" = samples where total CPU is well above its median; 1.0 for steady workloads.
    active_fraction: float
    burstiness: float
    cpu_active_p50: float
    cpu_active_p95: float
    mem_pod_p50_mib: float
    mem_pod_p95_mib: float
    mem_pod_p99_mib: float
    mem_pod_max_mib: float
    mem_active_p95_mib: float
    mem_growth_mib_per_day: float
    days_to_mem_limit: float | None
    restarts: int
    weekday_hourly_cpu: list[float]
    weekend_hourly_cpu: list[float]
    peaks: list[PeakWindow]
    signals: list[str]


def _pct(a: np.ndarray, q: float) -> float:
    return float(np.percentile(a, q)) if a.size else 0.0


def _hourly(values: np.ndarray, hour_idx: np.ndarray, mask: np.ndarray) -> np.ndarray:
    sums = np.bincount(hour_idx[mask], weights=values[mask], minlength=24)
    counts = np.bincount(hour_idx[mask], minlength=24)
    return np.divide(sums, counts, out=np.zeros(24), where=counts > 0)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs, start = [], None
    for i, hot in enumerate(list(mask) + [False]):
        if hot and start is None:
            start = i
        elif not hot and start is not None:
            runs.append((start, i))
            start = None
    return runs


def _hot(profile: np.ndarray) -> tuple[np.ndarray, float, float]:
    base = float(np.median(profile))
    threshold = max(2 * base, base + 0.25)
    return profile > threshold, base, threshold


def find_peaks(cpu_total: np.ndarray, hour_idx: np.ndarray, weekday: np.ndarray, day: np.ndarray,
               weekday_prof: np.ndarray, weekend_prof: np.ndarray, min_recurrence: float = 0.6) -> list[PeakWindow]:
    """Recurring hour-of-day load peaks. One-off spikes are rejected by the recurrence check."""
    wd_hot, wd_base, wd_thr = _hot(weekday_prof)
    we_hot, we_base, we_thr = _hot(weekend_prof)
    peaks = []
    for start, end in _runs(wd_hot | we_hot):
        on_wd, on_we = bool(wd_hot[start:end].any()), bool(we_hot[start:end].any())
        days = "*" if on_wd and on_we else ("1-5" if on_wd else "0,6")
        day_ok = np.ones_like(weekday, dtype=bool) if days == "*" else ((weekday < 5) if on_wd else (weekday >= 5))
        sel = day_ok & (hour_idx >= start) & (hour_idx < end)
        threshold = min(wd_thr if on_wd else np.inf, we_thr if on_we else np.inf)
        per_day = np.bincount(day[sel], weights=cpu_total[sel]) / np.maximum(np.bincount(day[sel]), 1)
        seen = np.bincount(day[sel]) > 0
        recurrence = float((per_day[seen] > threshold).mean()) if seen.any() else 0.0
        if recurrence < min_recurrence:
            continue
        peaks.append(PeakWindow(days=days, start_hour=start, end_hour=end,
                                peak_total_cpu=round(_pct(cpu_total[sel], 95), 3),
                                baseline_total_cpu=round(wd_base if on_wd else we_base, 3),
                                recurrence=round(recurrence, 2)))
    return peaks


def memory_growth(ts: np.ndarray, mem_mib: np.ndarray, restarts: np.ndarray) -> float:
    """Median MiB/day slope between container restarts (segments shorter than a day are ignored)."""
    cuts = (np.where(np.diff(restarts) > 0)[0] + 1).tolist()
    slopes = []
    for a, b in zip([0, *cuts], [*cuts, len(ts)]):
        if b - a >= 2 and ts[b - 1] - ts[a] >= 86400:
            slopes.append(np.polyfit((ts[a:b] - ts[a]) / 86400, mem_mib[a:b], 1)[0])
    return float(np.median(slopes)) if slopes else 0.0


def summarize(*, namespace: str, name: str, ts: np.ndarray, cpu_pod: np.ndarray, cpu_total: np.ndarray,
              mem_pod: np.ndarray, restarts: np.ndarray, replicas: int, cpu_request: float | None,
              cpu_limit: float | None, mem_request: float | None, mem_limit: float | None, tz: str) -> WorkloadStats:
    """Inputs are aligned arrays (NaN = missing); memory in bytes, CPU in cores."""
    ok = np.isfinite(cpu_pod) & np.isfinite(cpu_total) & np.isfinite(mem_pod)
    ts, cpu_pod, cpu_total, mem = ts[ok], cpu_pod[ok], cpu_total[ok], mem_pod[ok] / MI
    restarts = np.maximum.accumulate(np.nan_to_num(restarts[ok], nan=0.0))
    if ts.size < 2:
        raise ValueError(f"not enough samples for {namespace}/{name}")
    hour, weekday, day = local_parts(ts, tz)
    hour_idx = hour.astype(int) % 24

    median_total, p99_total = float(np.median(cpu_total)), _pct(cpu_total, 99)
    bursty = p99_total > 2 * max(median_total, 1e-3)
    active = cpu_total > median_total + 0.5 * (p99_total - median_total) if bursty else np.ones(ts.size, bool)

    weekday_prof = _hourly(cpu_total, hour_idx, weekday < 5)
    weekend_prof = _hourly(cpu_total, hour_idx, weekday >= 5)
    peaks = find_peaks(cpu_total, hour_idx, weekday, day, weekday_prof, weekend_prof)

    mem_limit_mib = mem_limit / MI if mem_limit else None
    mem_request_mib = mem_request / MI if mem_request else None
    growth = memory_growth(ts, mem, restarts)
    days_to_limit = None
    if mem_limit_mib and growth > 1:
        days_to_limit = round(max(0.0, (mem_limit_mib - float(mem[-1])) / growth), 1)
    n_restarts = int(restarts[-1] - restarts[0])

    s = dict(
        id=f"{namespace}/{name}", namespace=namespace, name=name,
        window_days=round(float(ts[-1] - ts[0]) / 86400, 1), replicas=replicas,
        cpu_request_cores=cpu_request, cpu_limit_cores=cpu_limit,
        mem_request_mib=mem_request_mib, mem_limit_mib=mem_limit_mib,
        cpu_pod_p50=_pct(cpu_pod, 50), cpu_pod_p95=_pct(cpu_pod, 95), cpu_pod_p99=_pct(cpu_pod, 99),
        cpu_pod_max=float(cpu_pod.max()), cpu_total_p95=_pct(cpu_total, 95), cpu_total_max=float(cpu_total.max()),
        cpu_over_request_pct=100 * float((cpu_pod > cpu_request).mean()) if cpu_request else 0.0,
        active_fraction=float(active.mean()), burstiness=p99_total / max(median_total, 1e-3),
        cpu_active_p50=_pct(cpu_pod[active], 50), cpu_active_p95=_pct(cpu_pod[active], 95),
        mem_pod_p50_mib=_pct(mem, 50), mem_pod_p95_mib=_pct(mem, 95), mem_pod_p99_mib=_pct(mem, 99),
        mem_pod_max_mib=float(mem.max()), mem_active_p95_mib=_pct(mem[active], 95),
        mem_growth_mib_per_day=growth, days_to_mem_limit=days_to_limit, restarts=n_restarts,
        weekday_hourly_cpu=[round(float(v), 3) for v in weekday_prof],
        weekend_hourly_cpu=[round(float(v), 3) for v in weekend_prof],
        peaks=peaks,
    )
    for key, value in s.items():
        if isinstance(value, float):
            s[key] = round(value, 1 if key.endswith("_mib") or key.endswith("_per_day") else 3)
    s["signals"] = _signals(s)
    return WorkloadStats(**s)


def _signals(s: dict) -> list[str]:
    """Deterministic tags. The triage model sees them, but makes its own call."""
    sig = []
    cpu_req, mem_req, mem_lim = s["cpu_request_cores"], s["mem_request_mib"], s["mem_limit_mib"]
    if cpu_req is None or mem_req is None:
        sig.append("no-requests")
    if cpu_req and s["cpu_pod_p95"] < 0.4 * cpu_req:
        sig.append("cpu-overprovisioned")
    if mem_req and s["mem_pod_p99_mib"] < 0.5 * mem_req:
        sig.append("mem-overprovisioned")
    if mem_req and s["mem_pod_p95_mib"] > mem_req:
        sig.append("mem-over-request")
    if s["cpu_over_request_pct"] > 2:
        sig.append("cpu-over-request")
    if s["active_fraction"] < 0.25 and s["cpu_pod_p50"] < 0.05 * max(s["cpu_active_p50"], 1e-3):
        sig.append("batch-pattern")
    if s["peaks"]:
        sig.append("recurring-peak")
    if s["mem_growth_mib_per_day"] > 0.02 * (mem_lim or mem_req or s["mem_pod_max_mib"]):
        sig.append("memory-growth")
    if s["restarts"] > 0:
        sig.append("oom-restarts" if mem_lim and s["mem_pod_max_mib"] >= 0.95 * mem_lim else "restarts")
    return sig
