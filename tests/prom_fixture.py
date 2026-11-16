"""A stand-in for Prometheus that answers, from simulated series, the queries right-sizing and the cluster
inventory make. It knows those queries by the metric they read, not PromQL in general: enough to run
collect(), cluster.inventory() and a whole analysis without a database."""
from __future__ import annotations

import re

import numpy as np

from kubesage.simulate.backfill import generate
from kubesage.simulate.fleet import generate_fleet
from kubesage.simulate.scenarios import DEMO_WORKLOADS

TZ = "Asia/Colombo"
END = 1_790_000_000 // 300 * 300


def cluster_series(days: int = 3, end: int = END) -> list:
    """The demo namespace and the fleet around it, as `kubesage simulate` writes them."""
    return (generate(DEMO_WORKLOADS, namespace="demo", days=days, end=end, step=60, tz=TZ, seed=42)
            + generate_fleet(days=days, end=end, tz=TZ, seed=42))


class SeriesProm:
    def __init__(self, series, *, drop: tuple[str, ...] = ()):
        self.series = [s for s in series if s.labels["__name__"] not in drop]
        self.calls: list[dict] = []  # PromClient keeps its requests here for the trace
        self.asked: list[str] = []

    def _named(self, query: str, metric: str):
        namespace = re.search(r'namespace="([^"]+)"', query)
        resource = re.search(r'resource="([^"]+)"', query)
        for s in self.series:
            m = s.labels
            if m["__name__"] != metric or (namespace and m.get("namespace") != namespace.group(1)):
                continue
            if resource and m.get("resource") != resource.group(1):
                continue
            yield s

    @staticmethod
    def _at(s, t: float, lookback: float = 300):
        """The series' value at t: its newest sample no older than the lookback, as Prometheus reads an instant."""
        ts = s.timestamps_ms / 1000
        i = int(np.searchsorted(ts, t, side="right")) - 1
        return float(s.values[i]) if i >= 0 and t - ts[i] <= lookback else None

    def query_range(self, query: str, start: float, end: float, step: int) -> list[dict]:
        self.asked.append(query)
        grid = np.arange(start, end + 1, step)
        if query.startswith("count(") and "kube_pod_container_resource_requests" in query:
            values = [[t, str(n)] for t in grid
                      if (n := sum(self._at(s, t, 600) is not None for s in self._named(query, "kube_pod_container_resource_requests")))]
            return [{"metric": {}, "values": values}] if values else []
        out = []
        for metric, rate in (("container_cpu_usage_seconds_total", True), ("container_memory_working_set_bytes", False),
                             ("kube_pod_container_status_restarts_total", False)):
            if metric not in query:
                continue
            for s in self._named(query, metric):
                values = []
                for t in grid:
                    now = self._at(s, t)
                    before = self._at(s, t - 300) if rate else 0.0
                    if now is not None and before is not None:
                        values.append([t, str((now - before) / 300 if rate else now)])
                if values:
                    out.append({"metric": {"pod": s.labels["pod"], "container": s.labels["container"]}, "values": values})
        return out

    def query(self, query: str, at: float | None = None) -> list[dict]:
        self.asked.append(query)
        at = END if at is None else at
        window = re.search(r"\[(\d+)([dm])\]", query)
        lookback = int(window.group(1)) * {"d": 86400, "m": 60}[window.group(2)] if window else 300
        by = re.search(r"by \(([^)]*)\)", query)
        keys = [k.strip() for k in by.group(1).split(",")] if by else []
        metric = next((name for name in sorted({s.labels["__name__"] for s in self.series}, key=len, reverse=True)
                       if name in query), None)
        if metric is None:
            return []
        rows: dict[tuple, float] = {}
        for s in self._named(query, metric):
            value = self._at(s, at, lookback)
            if value is not None:
                key = tuple(s.labels.get(k, "") for k in keys)
                rows[key] = max(rows.get(key, value), value)
        if query.startswith("count(") or query.startswith("count by"):
            if not keys:
                return [{"metric": {}, "value": [at, str(len(rows) or 0)]}] if rows else []
            return [{"metric": dict(zip(keys, key)), "value": [at, "1"]} for key in sorted(rows)]
        return [{"metric": dict(zip(keys, key)), "value": [at, str(v)]} for key, v in sorted(rows.items())]
