"""Turn scenario curves into cAdvisor / kube-state-metrics shaped series and backfill VictoriaMetrics."""
from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import httpx
import numpy as np

from kubesage.simulate.scenarios import TimeAxis, Workload
from kubesage.timeutil import local_parts

SPEC_EVERY = 5  # constant spec metrics (requests, limits, replicas) are written every 5th sample


@dataclass
class Series:
    labels: dict[str, str]
    timestamps_ms: np.ndarray
    values: np.ndarray


def time_axis(start: int, end: int, step: int, tz: str) -> TimeAxis:
    ts = np.arange(start, end + 1, step, dtype=np.int64)
    hour, weekday, day = local_parts(ts, tz)
    return TimeAxis(ts=ts, hour=hour, weekday=weekday, day=day)


def generate(workloads: Iterable[Workload], *, namespace: str, days: int, end: int, step: int,
             tz: str, seed: int) -> list[Series]:
    t = time_axis(end - days * 86400, end, step, tz)
    ts_ms = t.ts * 1000
    spec_ms = ts_ms[::SPEC_EVERY]
    rng = np.random.default_rng(seed)
    out: list[Series] = []

    def spec(name: str, labels: dict[str, str], value: float) -> Series:
        return Series({"__name__": name, **labels}, spec_ms, np.full(len(spec_ms), value))

    for w in workloads:
        template_hash = f"{rng.integers(16**10):010x}"[:10]
        out.append(spec("kube_deployment_spec_replicas",
                        {"job": "kube-state-metrics", "namespace": namespace, "deployment": w.name}, w.replicas))
        for _ in range(w.replicas):
            pod = f"{w.name}-{template_hash}-{''.join(rng.choice(list('bcdfghjklmnpqrstvwxz2456789'), 5))}"
            cpu, mem, restarts = w.pattern(t, w, np.random.default_rng(rng.integers(2**32)))
            labels = {"namespace": namespace, "pod": pod, "container": w.name}
            kubelet = {"job": "kubelet", **labels}
            ksm = {"job": "kube-state-metrics", **labels}
            out += [
                Series({"__name__": "container_cpu_usage_seconds_total", **kubelet}, ts_ms, np.cumsum(cpu * step)),
                Series({"__name__": "container_memory_working_set_bytes", **kubelet}, ts_ms, np.round(mem)),
                Series({"__name__": "kube_pod_container_status_restarts_total", **ksm}, ts_ms, restarts),
            ]
            for kind, cpu_v, mem_v in (("requests", w.cpu_request, w.mem_request), ("limits", w.cpu_limit, w.mem_limit)):
                metric = f"kube_pod_container_resource_{kind}"
                if cpu_v is not None:
                    out.append(spec(metric, {**ksm, "resource": "cpu", "unit": "core"}, cpu_v))
                if mem_v is not None:
                    out.append(spec(metric, {**ksm, "resource": "memory", "unit": "byte"}, mem_v))
    return out


def to_jsonl(series: Iterable[Series]) -> Iterator[str]:
    """VictoriaMetrics /api/v1/import line format."""
    for s in series:
        yield json.dumps({"metric": s.labels, "values": np.round(s.values, 4).tolist(),
                          "timestamps": s.timestamps_ms.tolist()}, separators=(",", ":"))


def import_to_vm(vm_url: str, series: list[Series], *, namespace: str, replace: bool = True,
                 record: list[dict] | None = None) -> int:
    """Backfill series into VictoriaMetrics and make them immediately queryable. Returns sample count.
    `record` receives each HTTP request as sent (the import body as a short preview and its size)."""
    lines = list(to_jsonl(series))
    body = gzip.compress("\n".join(lines).encode(), compresslevel=5)

    def call(http: httpx.Client, method: str, path: str, **kw) -> None:
        started = time.perf_counter()
        resp = http.request(method, path, **kw)
        if record is not None:
            entry = {"kind": "vm", "stage": 1, "request": {"method": method, "url": vm_url.rstrip("/") + path,
                                                          "params": kw.get("params"), "headers": kw.get("headers")},
                     "response": {"status": resp.status_code}, "latency_ms": int((time.perf_counter() - started) * 1000)}
            if "content" in kw:
                entry["request"]["body_preview"] = [preview_line(line) for line in lines[:2]]
                entry["request"]["body_bytes"] = {"gzip": len(body), "raw": sum(len(line) + 1 for line in lines),
                                                  "series": len(lines)}
            record.append(entry)
        resp.raise_for_status()

    with httpx.Client(base_url=vm_url.rstrip("/"), timeout=120) as http:
        if replace:
            # Re-running the simulator must not interleave two datasets under the same labels.
            call(http, "POST", "/api/v1/admin/tsdb/delete_series", params={"match[]": f'{{namespace="{namespace}"}}'})
        call(http, "POST", "/api/v1/import", content=body, headers={"Content-Encoding": "gzip"})
        call(http, "GET", "/internal/force_flush")
        call(http, "GET", "/internal/resetRollupResultCache")
    return sum(len(s.values) for s in series)


def preview_line(line: str, keep: int = 3) -> dict:
    """One import line with its value/timestamp arrays cut to `keep` items, for display."""
    item = json.loads(line)
    total = len(item["values"])
    return {"metric": item["metric"], "values": item["values"][:keep], "timestamps": item["timestamps"][:keep],
            "points": total}
