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


def collect(client: PromClient, *, namespace: str, days: int, end: float, tz: str,
            step: int = 300) -> tuple[list[WorkloadStats], int]:
    """Return per-workload stats and the number of raw (timestamp, value) points they were built from."""
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

    spec: dict[str, dict[str, float]] = defaultdict(dict)
    for kind in ("requests", "limits"):
        q = f"max by (namespace, container, resource) (last_over_time(kube_pod_container_resource_{kind}{{{sel}}}[{days}d]))"
        for r in client.query(q, at=end):
            spec[r["metric"]["container"]][f"{r['metric']['resource']}_{kind}"] = float(r["value"][1])
    replicas = {r["metric"]["deployment"]: int(float(r["value"][1])) for r in client.query(
        f'max by (deployment) (last_over_time(kube_deployment_spec_replicas{{namespace="{namespace}"}}[{days}d]))', at=end)}

    stats = []
    for name in sorted(cpu_pod):
        if name not in cpu_total or name not in mem_pod:
            continue
        sp = spec.get(name, {})
        stats.append(summarize(
            namespace=namespace, name=name, ts=grid, cpu_pod=cpu_pod[name], cpu_total=cpu_total[name],
            mem_pod=mem_pod[name], restarts=restarts.get(name, np.zeros(n)), replicas=replicas.get(name, 1),
            cpu_request=sp.get("cpu_requests"), cpu_limit=sp.get("cpu_limits"),
            mem_request=sp.get("memory_requests"), mem_limit=sp.get("memory_limits"), tz=tz))
    raw_points = int(sum(np.isfinite(cpu_total[s.name]).sum() + np.isfinite(mem_pod[s.name]).sum() for s in stats))
    return stats, raw_points
