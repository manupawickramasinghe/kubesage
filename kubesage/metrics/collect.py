"""Pull a namespace's usage history out of Prometheus/VictoriaMetrics and reduce it to WorkloadStats.

One record per Deployment or StatefulSet, found through the pod owner references kube-state-metrics exports (or
the pod names, without them). The record describes the pod's **main container**: the one that reserves and uses
the most. A sidecar is named on the record and left alone: its requests still count towards the node pool
(metrics/cluster.py), but nothing here resizes it. DaemonSets, Jobs and bare pods are not right-sized: their
replica count is not theirs to set, or they don't live long enough to have a history.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from kubesage.journal import log
from kubesage.metrics import owners as own
from kubesage.metrics.client import PromClient
from kubesage.metrics.stats import WorkloadStats, summarize

GIB = 2**30
MIN_SAMPLES = 12  # an hour at the default 5-minute step
RIGHT_SIZED = ("Deployment", "StatefulSet")
REPLICA_METRIC = {"Deployment": ("kube_deployment_spec_replicas", "deployment"),
                  "StatefulSet": ("kube_statefulset_replicas", "statefulset")}
CPU_TO_GIB = 7.5  # one core weighs as much as this many GiB when picking the main container


def _align(values: list, start: int, step: int, n: int) -> np.ndarray:
    out = np.full(n, np.nan)
    for ts, v in values:
        i = round((float(ts) - start) / step)
        if 0 <= i < n:
            out[i] = float(v)
    return out


def _across(rows: list[np.ndarray], how: str) -> np.ndarray:
    """Max or sum across pods at each instant; NaN where no pod has a sample."""
    stack = np.vstack(rows)
    seen = np.isfinite(stack).any(axis=0)
    filled = np.where(np.isfinite(stack), stack, -np.inf if how == "max" else 0.0)
    return np.where(seen, filled.max(axis=0) if how == "max" else filled.sum(axis=0), np.nan)


def _last_seen(row: np.ndarray) -> int:
    idx = np.flatnonzero(np.isfinite(row))
    return int(idx[-1]) if idx.size else -1


def collect(client: PromClient, *, namespace: str, days: int, end: float, tz: str, step: int = 300,
            owners: own.Owners | None = None) -> tuple[list[WorkloadStats], int]:
    """Return per-workload stats and the number of raw (timestamp, value) points they were built from."""
    now = end  # instant queries use the real time; the range grid is aligned to the step
    end = int(end) // step * step
    start = end - days * 86400
    n = (end - start) // step + 1
    grid = np.arange(start, end + 1, step, dtype=np.float64)
    sel = f'namespace="{namespace}", container!="", container!="POD"'
    by = "by (pod, container)"

    def series(q: str) -> dict[tuple[str, str], np.ndarray]:
        return {(r["metric"].get("pod", ""), r["metric"].get("container", "")): _align(r["values"], start, step, n)
                for r in client.query_range(q, start, end, step)}

    cpu = series(f"max {by} (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))")
    mem = series(f"max {by} (container_memory_working_set_bytes{{{sel}}})")
    restarts = series(f"max {by} (kube_pod_container_status_restarts_total{{{sel}}})")

    def current(metric: str, by_labels: str) -> str:
        """Live value if the series is being scraped right now, else the last value in the window."""
        return f"(max by ({by_labels}) ({metric})) or (max by ({by_labels}) (last_over_time({metric}[{days}d])))"

    spec: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for kind in ("requests", "limits"):
        q = current(f"kube_pod_container_resource_{kind}{{{sel}}}", "pod, container, resource")
        for r in client.query(q, at=now):
            m = r["metric"]
            spec[(m.get("pod", ""), m.get("container", ""))][f"{m.get('resource')}_{kind}"] = float(r["value"][1])
    replicas: dict[tuple[str, str], int] = {}
    for kind, (metric, label) in REPLICA_METRIC.items():
        for r in client.query(current(f'{metric}{{namespace="{namespace}"}}', label), at=now):
            replicas[(kind, r["metric"].get(label, ""))] = int(float(r["value"][1]))
    if owners is None:
        owners = own.fetch(client, at=now, days=days, namespace=namespace)

    # workload -> container -> pod, for every (pod, container) with both a CPU and a memory series
    groups: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    skipped: dict[tuple[str, str], int] = defaultdict(int)
    for pod, container in sorted(set(cpu) & set(mem)):
        workload = own.workload_of(namespace, pod, owners)
        if workload[0] in RIGHT_SIZED:
            groups[workload][container].append(pod)
        else:
            skipped[workload] += 1
    for (kind, name), pods in sorted(skipped.items()):
        log("collect", f"not right-sized: {namespace}/{name} is a {kind} ({pods} pod series)", stage=1)

    stats, raw_points = [], 0
    for (kind, name), containers in sorted(groups.items(), key=lambda kv: kv[0][1]):
        # A request counts as the workload's when it is the newest pods': a request that was just lowered must not
        # be hidden by the old pods it replaced.
        def newest(container: str, key: str) -> float | None:
            pods = containers[container]
            last = max(_last_seen(cpu[(p, container)]) for p in pods)
            values = [spec[(p, container)][key] for p in pods
                      if _last_seen(cpu[(p, container)]) >= last - 1 and key in spec.get((p, container), {})]
            return max(values) if values else None

        def weight(container: str) -> float:
            pods = containers[container]
            used_cpu = float(np.nanmean(_across([cpu[(p, container)] for p in pods], "max")))
            used_mem = float(np.nanmean(_across([mem[(p, container)] for p in pods], "max"))) / GIB
            return (max(newest(container, "cpu_requests") or 0.0, used_cpu) * CPU_TO_GIB
                    + max((newest(container, "memory_requests") or 0.0) / GIB, used_mem))

        main = max(sorted(containers), key=weight)
        pods = containers[main]
        cpu_pod = _across([cpu[(p, main)] for p in pods], "max")
        cpu_total = _across([cpu[(p, main)] for p in pods], "sum")
        mem_pod = _across([mem[(p, main)] for p in pods], "max")
        seen = np.isfinite(cpu_pod) & np.isfinite(cpu_total) & np.isfinite(mem_pod)
        if seen.sum() < MIN_SAMPLES:  # a debug pod or a just-deployed workload: no history worth analysing yet
            log("collect", f"skipped {namespace}/{name}: {int(seen.sum())} samples, needs {MIN_SAMPLES}", stage=1)
            continue
        counted = [restarts[(p, main)] for p in pods if (p, main) in restarts]
        last = max(_last_seen(cpu[(p, main)]) for p in pods)
        alive = sum(_last_seen(cpu[(p, main)]) >= last - 1 for p in pods)
        stats.append(summarize(
            namespace=namespace, name=name, ts=grid, cpu_pod=cpu_pod, cpu_total=cpu_total, mem_pod=mem_pod,
            restarts=_across(counted, "max") if counted else np.zeros(n),
            replicas=replicas.get((kind, name), max(alive, 1)),
            cpu_request=newest(main, "cpu_requests"), cpu_limit=newest(main, "cpu_limits"),
            mem_request=newest(main, "memory_requests"), mem_limit=newest(main, "memory_limits"), tz=tz,
            kind=kind, container=main, sidecars=[c for c in containers if c != main]))
        raw_points += int(np.isfinite(cpu_total).sum() + np.isfinite(mem_pod).sum())
    return stats, raw_points
