"""Turn simulated series into the arrays costs.fetch() would read from a database, so the whole cost report can be
tested (and the browser checks fed) without one. It mirrors the queries: per-day counter increase for CPU, and
per-day sums of the gauges, in core-hours and GiB-hours."""
from __future__ import annotations

import numpy as np

from kubesage.costs import DAY, GIB, Node, PodUsage, RawCosts, Volume
from kubesage.simulate.backfill import generate
from kubesage.simulate.fleet import generate_fleet
from kubesage.simulate.scenarios import DEMO_WORKLOADS

TZ = "Asia/Colombo"
END = 1_790_000_000 // 300 * 300


def _daily(s, days: int, end: int, *, counter: bool = False) -> np.ndarray:
    ts = s.timestamps_ms / 1000
    out = np.zeros(days)
    if len(ts) == 0:
        return out
    step = float(np.median(np.diff(ts))) if len(ts) > 1 else 300.0
    bucket = np.ceil((ts - (end - days * DAY)) / DAY).astype(int) - 1
    for i in range(days):
        inside = bucket == i
        if not inside.any():
            continue
        if counter:
            before = s.values[bucket < i]
            out[i] = s.values[inside][-1] - (before[-1] if len(before) else s.values[inside][0])
        else:
            out[i] = float(s.values[inside].mean()) * min(inside.sum() * step / 3600, 24.0)
    return out


def raw_from_series(series, *, days: int, end: int) -> RawCosts:
    raw = RawCosts(days=days, end=end)
    zero = np.zeros(days)
    parts: dict[tuple[str, str], dict[str, np.ndarray]] = {}
    nodes: dict[str, Node] = {}
    volumes: dict[tuple[str, str], Volume] = {}
    for s in series:
        m, name = s.labels, s.labels["__name__"]
        pod = (m.get("namespace", ""), m.get("pod", ""))
        if name == "container_cpu_usage_seconds_total":
            parts.setdefault(pod, {})["cpu_used"] = parts.get(pod, {}).get("cpu_used", zero) + _daily(s, days, end, counter=True) / 3600
        elif name == "container_memory_working_set_bytes":
            parts.setdefault(pod, {})["mem_used"] = parts.get(pod, {}).get("mem_used", zero) + _daily(s, days, end) / GIB
        elif name == "kube_pod_container_resource_requests":
            key, scale = ("cpu_req", 1) if m["resource"] == "cpu" else ("mem_req", GIB)
            parts.setdefault(pod, {})[key] = parts.get(pod, {}).get(key, zero) + _daily(s, days, end) / scale
        elif name == "kube_pod_owner":
            raw.owners[pod] = (m["owner_kind"], m["owner_name"])
        elif name == "kube_replicaset_owner":
            raw.rs_owners[(m["namespace"], m["replicaset"])] = (m["owner_kind"], m["owner_name"])
        elif name == "kube_pod_labels" and m.get("label_team"):
            raw.pod_teams[pod] = m["label_team"]
        elif name == "kube_node_info":
            nodes.setdefault(m["node"], Node(m["node"], zero)).hours = _daily(s, days, end)
        elif name == "kube_node_labels":
            nodes.setdefault(m["node"], Node(m["node"], zero)).sku = m.get("label_kubesage_io_sku")
        elif name == "kube_node_status_capacity":
            node = nodes.setdefault(m["node"], Node(m["node"], zero))
            if m["resource"] == "cpu":
                node.cpu = float(s.values[-1])
            else:
                node.mem_gib = float(s.values[-1]) / GIB
        elif name == "kube_persistentvolumeclaim_resource_requests_storage_bytes":
            key = (m["namespace"], m["persistentvolumeclaim"])
            volumes.setdefault(key, Volume(*key, zero)).gib_h = _daily(s, days, end) / GIB
        elif name == "kube_pod_spec_volumes_persistentvolumeclaims_info":
            key = (m["namespace"], m["persistentvolumeclaim"])
            volumes.setdefault(key, Volume(*key, zero)).pods.add(m["pod"])
    for pod, p in sorted(parts.items()):
        raw.pods[pod] = PodUsage(p.get("cpu_used", zero), p.get("cpu_req", zero), p.get("mem_used", zero), p.get("mem_req", zero))
    raw.nodes = [nodes[k] for k in sorted(nodes)]
    raw.volumes = [volumes[k] for k in sorted(volumes)]
    return raw


def simulated_cluster(days: int = 30, end: int = END) -> RawCosts:
    """The demo namespace plus the fleet around it: what `kubesage simulate` writes."""
    series = generate(DEMO_WORKLOADS, namespace="demo", days=days, end=end, step=300, tz=TZ, seed=42)
    return raw_from_series(series + generate_fleet(days=days, end=end, tz=TZ, seed=42), days=days, end=end)
