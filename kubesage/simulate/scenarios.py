"""Demo workloads, each with a classic real-world resource anti-pattern baked into its usage curve."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

MI = 1024**2
GI = 1024**3


@dataclass(frozen=True)
class TimeAxis:
    ts: np.ndarray       # epoch seconds
    hour: np.ndarray     # local fractional hour, 0-24
    weekday: np.ndarray  # local weekday, Mon=0
    day: np.ndarray      # local day index from the first sample


# A pattern returns one pod's (cpu cores, memory bytes, cumulative restarts) per sample.
Pattern = Callable[[TimeAxis, "Workload", np.random.Generator], tuple[np.ndarray, np.ndarray, np.ndarray]]


@dataclass(frozen=True)
class Workload:
    name: str
    replicas: int
    cpu_request: float
    cpu_limit: float | None
    mem_request: int
    mem_limit: int | None
    pattern: Pattern
    story: str


def _diurnal(hour: np.ndarray, peak: float = 15.0) -> np.ndarray:
    """0 at (peak - 12h), 1 at peak."""
    return 0.5 * (1 - np.cos(2 * np.pi * (hour - peak + 12) / 24))


def overprovisioned(t: TimeAxis, w: Workload, rng: np.random.Generator):
    n = len(t.ts)
    cpu = 0.10 + 0.08 * _diurnal(t.hour) + rng.normal(0, 0.01, n)
    mem = 440 * MI + 20 * MI * _diurnal(t.hour) + rng.normal(0, 5 * MI, n)
    return np.clip(cpu, 0.005, w.cpu_limit), mem, np.zeros(n)


def nightly_batch(t: TimeAxis, w: Workload, rng: np.random.Generator):
    n = len(t.ts)
    run_hours = rng.uniform(0.6, 0.85, int(t.day.max()) + 1)[t.day]
    running = (t.hour >= 2.0) & (t.hour < 2.0 + run_hours)
    cpu = np.where(running, 1.6 + 0.2 * rng.random(n), 0.008 + rng.normal(0, 0.002, n))
    mem = np.where(running, 1.2 * GI + rng.normal(0, 30 * MI, n), 110 * MI + rng.normal(0, 3 * MI, n))
    return np.clip(cpu, 0.001, w.cpu_limit), mem, np.zeros(n)


def memory_leak(t: TimeAxis, w: Workload, rng: np.random.Generator):
    n = len(t.ts)
    business = np.where(t.weekday < 5, 1.0, 0.5)
    cpu = 0.06 + 0.10 * _diurnal(t.hour) * business + rng.normal(0, 0.01, n)
    step_days = np.diff(t.ts, prepend=t.ts[0]) / 86400
    base = 280 * MI
    level = base + rng.uniform(0, 0.5) * (w.mem_limit - base)
    leak = rng.uniform(95, 130) * MI  # bytes per day, re-rolled after every OOM kill
    mem, restarts, count = np.empty(n), np.empty(n), 0
    for i in range(n):
        level += leak * step_days[i]
        if level >= w.mem_limit:
            mem[i] = w.mem_limit
            level, count, leak = base, count + 1, rng.uniform(95, 130) * MI
        else:
            mem[i] = level + rng.normal(0, 2 * MI)
        restarts[i] = count
    return np.clip(cpu, 0.005, w.cpu_limit), mem, restarts


def weekday_spike(t: TimeAxis, w: Workload, rng: np.random.Generator):
    n = len(t.ts)
    weekday = t.weekday < 5
    load = 0.05 + 0.12 * _diurnal(t.hour, peak=14) * np.where(weekday, 1.0, 0.4)
    # Mon-Fri 09:00 rush: sharp ramp-up, short decay at 11:00.
    rush = np.clip((t.hour - 9.0) / 0.15, 0, 1) * np.clip((11.0 - t.hour) / 0.5, 0, 1)
    load = load + np.where(weekday, 0.8 * rush, 0.0)
    cpu = load * rng.uniform(0.9, 1.1) + rng.normal(0, 0.015, n)
    mem = 260 * MI + 120 * MI * load + rng.normal(0, 4 * MI, n)
    return np.clip(cpu, 0.005, w.cpu_limit), mem, np.zeros(n)


def healthy(t: TimeAxis, w: Workload, rng: np.random.Generator):
    n = len(t.ts)
    cpu = 0.12 + 0.06 * _diurnal(t.hour) + rng.normal(0, 0.012, n)
    mem = 170 * MI + 6 * MI * _diurnal(t.hour) + rng.normal(0, 3 * MI, n)
    return np.clip(cpu, 0.005, w.cpu_limit), mem, np.zeros(n)


# Requests are sized so the same manifests still schedule on a single-node kind cluster.
DEMO_WORKLOADS: list[Workload] = [
    Workload("payment-service", 1, 2.0, 2.0, 4 * GI, 4 * GI, overprovisioned,
             "Sized for a launch-day load test that never came back: uses ~0.2 of 2 cores."),
    Workload("report-worker", 1, 2.0, 2.0, 2 * GI, 2 * GI, nightly_batch,
             "Idles all day, runs a ~45 min batch at 02:00. Holds 2 cores 24/7."),
    Workload("auth-api", 1, 0.25, 0.5, 512 * MI, 1 * GI, memory_leak,
             "Leaks ~110 MiB/day until OOM-killed at its 1 GiB limit, every ~6 days."),
    Workload("checkout-web", 3, 0.5, 1.0, 512 * MI, 768 * MI, weekday_spike,
             "Mon-Fri 09:00 rush pins pods at their CPU limit; idle-ish the rest of the week."),
    Workload("catalog-api", 2, 0.25, 0.5, 256 * MI, 384 * MI, healthy,
             "Control case: requests already match usage."),
]
