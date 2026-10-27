"""Pull a namespace's usage history out of Prometheus/VictoriaMetrics and reduce it to WorkloadStats.

Demo assumption: one container per Deployment, and the container is named after the Deployment.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from kubesage.metrics.client import PromClient
from kubesage.metrics.stats import WorkloadStats, summarize


def _align(values: list, start: int, step: int, n: int) -> np.ndarray:
    out = np.full(n, np.nan)
    for ts, v in values:
        i = round((float(ts) - start) / step)
        if 0 <= i < n:
            out[i] = float(v)
    return out


MIN_SAMPLES = 12  # an hour at the default 5-minute step


def collect(client: PromClient, *, namespace: str, days: int, end: float, tz: str,
            step: int = 300) -> tuple[list[WorkloadStats], int]:
    """Return per-workload stats and the number of raw (timestamp, value) points they were built from."""
    now = end  # instant queries use the real time; the range grid is aligned to the step
    end = int(end) // step * step
    start = end - days * 86400
    n = (end - start) // step + 1
    grid = np.arange(start, end + 1, step, dtype=np.float64)
    sel = f'namespace="{namespace}", container!="", container!="POD"'
    by = "by (namespace, container)"

    def series(q: str) -> dict[str, np.ndarray]:
        return {r["metric"]["container"]: _align(r["values"], start, step, n)
                for r in client.query_range(q, start, end, step)}

    cpu_pod = series(f"max {by} (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))")
    cpu_total = series(f"sum {by} (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))")
    mem_pod = series(f"max {by} (container_memory_working_set_bytes{{{sel}}})")
    restarts = series(f"max {by} (kube_pod_container_status_restarts_total{{{sel}}})")

    def current(metric: str, by_labels: str) -> str:
        """Live value if the series is being scraped right now, else the last value in the window. Without
        this, the max over old backfilled pods would hide a request that was just lowered."""
        return f"(max by ({by_labels}) ({metric})) or (max by ({by_labels}) (last_over_time({metric}[{days}d])))"

    spec: dict[str, dict[str, float]] = defaultdict(dict)
    for kind in ("requests", "limits"):
        q = current(f"kube_pod_container_resource_{kind}{{{sel}}}", "namespace, container, resource")
        for r in client.query(q, at=now):
            spec[r["metric"]["container"]][f"{r['metric']['resource']}_{kind}"] = float(r["value"][1])
    replicas = {r["metric"]["deployment"]: int(float(r["value"][1])) for r in client.query(
        current(f'kube_deployment_spec_replicas{{namespace="{namespace}"}}', "deployment"), at=now)}

    stats = []
    for name in sorted(cpu_pod):
        if name not in cpu_total or name not in mem_pod:
            continue
        seen = np.isfinite(cpu_pod[name]) & np.isfinite(cpu_total[name]) & np.isfinite(mem_pod[name])
        if seen.sum() < MIN_SAMPLES:  # a debug pod or a just-deployed workload: no history worth analysing yet
            continue
        sp = spec.get(name, {})
        stats.append(summarize(
            namespace=namespace, name=name, ts=grid, cpu_pod=cpu_pod[name], cpu_total=cpu_total[name],
            mem_pod=mem_pod[name], restarts=restarts.get(name, np.zeros(n)), replicas=replicas.get(name, 1),
            cpu_request=sp.get("cpu_requests"), cpu_limit=sp.get("cpu_limits"),
            mem_request=sp.get("memory_requests"), mem_limit=sp.get("memory_limits"), tz=tz))
    raw_points = int(sum(np.isfinite(cpu_total[s.name]).sum() + np.isfinite(mem_pod[s.name]).sum() for s in stats))
    return stats, raw_points
