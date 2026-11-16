"""The rest of a small shop's cluster, simulated, so cost allocation has more than one namespace to explain.

`scenarios.py` is the `demo` namespace right-sizing works on. This adds what sits around it in a real cluster:
a data tier with volumes, a staging copy, platform services, kube-system and monitoring, on two nodes, owned by
three teams. Four problems are baked in, one per finding cost allocation should make:

- `data/legacy-export` reserves half a core and has done nothing for weeks (an abandoned workload);
- `data/orders-db-backup-2025` is a 200 GiB volume no pod mounts;
- `platform/node-problem-detector` runs without requests;
- `staging/load-test` appears on the last day and triples the namespace's cost (a cost spike).

Every series carries `kubesage_sim="fleet"`, which is how a re-run replaces them without touching anything a
real cluster scraped. Owner references, team labels, node labels and capacity are written the way
kube-state-metrics exports them, so the same queries work on a real cluster.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from kubesage.simulate.backfill import Series, time_axis
from kubesage.simulate.scenarios import GI, MI, _diurnal

SIM_LABEL = {"kubesage_sim": "fleet"}
DELETE_MATCH = '{kubesage_sim="fleet"}'
STEP = 300


@dataclass(frozen=True)
class FleetWorkload:
    namespace: str
    name: str
    kind: str  # Deployment | StatefulSet | DaemonSet
    team: str | None
    replicas: int
    cpu_request: float | None
    mem_request: int | None
    cpu_used: float  # average cores per pod
    mem_used: int  # average bytes per pod
    swing: float = 0.3  # how much of the average follows the time of day
    last_hours: float | None = None  # only exists for the last N hours of the window
    volume_gib: int = 0  # one claim per pod, StatefulSet style


@dataclass(frozen=True)
class FleetNode:
    name: str
    sku: str
    cpu: int
    mem_gib: int


NODES = [FleetNode("aks-default-31250000-vmss000000", "D8s_v5", 8, 32),
         FleetNode("aks-default-31250000-vmss000001", "D8s_v5", 8, 32)]

FLEET: list[FleetWorkload] = [
    FleetWorkload("data", "orders-db", "StatefulSet", "data", 1, 1.0, 2 * GI, 0.42, int(1.6 * GI), volume_gib=50),
    FleetWorkload("data", "redis-cache", "Deployment", "data", 1, 0.25, 512 * MI, 0.11, 300 * MI),
    FleetWorkload("data", "legacy-export", "Deployment", "data", 1, 0.5, 1 * GI, 0.002, 64 * MI, swing=0.0),
    FleetWorkload("staging", "checkout-web", "Deployment", "storefront", 1, 0.25, 256 * MI, 0.04, 140 * MI),
    FleetWorkload("staging", "catalog-api", "Deployment", "storefront", 1, 0.1, 128 * MI, 0.03, 90 * MI),
    FleetWorkload("staging", "payment-service", "Deployment", "storefront", 1, 0.5, 1 * GI, 0.04, 380 * MI),
    FleetWorkload("staging", "load-test", "Deployment", "storefront", 3, 1.0, 512 * MI, 0.85, 300 * MI, swing=0.1,
                  last_hours=22),
    FleetWorkload("platform", "ingress-nginx-controller", "Deployment", "platform", 2, 0.2, 256 * MI, 0.12, 180 * MI),
    FleetWorkload("platform", "cert-manager", "Deployment", "platform", 1, 0.05, 128 * MI, 0.01, 70 * MI, swing=0.0),
    FleetWorkload("platform", "node-problem-detector", "DaemonSet", "platform", 2, None, None, 0.03, 80 * MI, swing=0.0),
    FleetWorkload("kube-system", "coredns", "Deployment", None, 2, 0.1, 70 * MI, 0.02, 40 * MI),
    FleetWorkload("kube-system", "metrics-server", "Deployment", None, 1, 0.1, 200 * MI, 0.03, 90 * MI, swing=0.0),
    FleetWorkload("monitoring", "victoriametrics", "StatefulSet", "platform", 1, 0.5, 1 * GI, 0.22, 700 * MI, volume_gib=20),
    FleetWorkload("monitoring", "grafana", "Deployment", "platform", 1, 0.1, 256 * MI, 0.02, 150 * MI),
]

# (namespace, claim, GiB): provisioned, mounted by nothing.
ORPHAN_VOLUMES = [("data", "orders-db-backup-2025", 200)]


def namespaces() -> list[str]:
    return sorted({w.namespace for w in FLEET})


def _suffix(rng: np.random.Generator, n: int) -> str:
    return "".join(rng.choice(list("bcdfghjklmnpqrstvwxz2456789"), n))


def generate_fleet(*, days: int, end: int, tz: str, seed: int = 42, step: int = STEP) -> list[Series]:
    """Usage, requests and the kube-state-metrics metadata for FLEET on NODES, `days` back from `end`."""
    t = time_axis(end - days * 86400, end, step, tz)
    ts_ms = t.ts * 1000
    rng = np.random.default_rng(seed + 1000)
    ksm = {"job": "kube-state-metrics", **SIM_LABEL}
    kubelet = {"job": "kubelet", **SIM_LABEL}
    out: list[Series] = []

    def const(name: str, labels: dict[str, str], value: float, mask: np.ndarray | None = None) -> None:
        stamps = ts_ms if mask is None else ts_ms[mask]
        out.append(Series({"__name__": name, **ksm, **labels}, stamps, np.full(len(stamps), value)))

    for node in NODES:
        const("kube_node_info", {"node": node.name}, 1)
        const("kube_node_labels", {"node": node.name, "label_kubesage_io_sku": node.sku,
                                   "label_node_kubernetes_io_instance_type": f"Standard_{node.sku}"}, 1)
        const("kube_node_status_capacity", {"node": node.name, "resource": "cpu", "unit": "core"}, node.cpu)
        const("kube_node_status_capacity", {"node": node.name, "resource": "memory", "unit": "byte"}, node.mem_gib * GI)

    for w in FLEET:
        alive = np.ones(len(t.ts), dtype=bool) if w.last_hours is None else t.ts >= end - w.last_hours * 3600
        template = _suffix(rng, 9)
        if w.kind == "Deployment":
            const("kube_replicaset_owner", {"namespace": w.namespace, "replicaset": f"{w.name}-{template}",
                                            "owner_kind": "Deployment", "owner_name": w.name}, 1, alive)
        for i in range(w.replicas):
            pod = {"Deployment": f"{w.name}-{template}-{_suffix(rng, 5)}", "StatefulSet": f"{w.name}-{i}",
                   "DaemonSet": f"{w.name}-{_suffix(rng, 5)}"}[w.kind]
            labels = {"namespace": w.namespace, "pod": pod}
            n = int(alive.sum())
            wave = 1 + w.swing * (2 * _diurnal(t.hour[alive]) - 1)
            cpu = np.clip(w.cpu_used * wave * rng.uniform(0.95, 1.05) + rng.normal(0, w.cpu_used * 0.05, n), 0.0005, None)
            mem = np.clip(w.mem_used * (1 + 0.1 * w.swing * (2 * _diurnal(t.hour[alive]) - 1))
                          + rng.normal(0, w.mem_used * 0.01, n), MI, None)
            box = {**labels, "container": w.name}
            out.append(Series({"__name__": "container_cpu_usage_seconds_total", **kubelet, **box}, ts_ms[alive],
                              np.cumsum(cpu * step)))
            out.append(Series({"__name__": "container_memory_working_set_bytes", **kubelet, **box}, ts_ms[alive], np.round(mem)))
            if w.cpu_request is not None:
                const("kube_pod_container_resource_requests", {**box, "resource": "cpu", "unit": "core"}, w.cpu_request, alive)
            if w.mem_request is not None:
                const("kube_pod_container_resource_requests", {**box, "resource": "memory", "unit": "byte"}, w.mem_request, alive)
            owner = (("ReplicaSet", f"{w.name}-{template}") if w.kind == "Deployment" else (w.kind, w.name))
            const("kube_pod_owner", {**labels, "owner_kind": owner[0], "owner_name": owner[1],
                                     "owner_is_controller": "true"}, 1, alive)
            if w.team:
                const("kube_pod_labels", {**labels, "label_team": w.team}, 1, alive)
            if w.volume_gib:
                claim = f"data-{w.name}-{i}"
                const("kube_persistentvolumeclaim_resource_requests_storage_bytes",
                      {"namespace": w.namespace, "persistentvolumeclaim": claim}, w.volume_gib * GI, alive)
                const("kube_pod_spec_volumes_persistentvolumeclaims_info",
                      {**labels, "volume": "data", "persistentvolumeclaim": claim}, 1, alive)

    for namespace, claim, gib in ORPHAN_VOLUMES:
        const("kube_persistentvolumeclaim_resource_requests_storage_bytes",
              {"namespace": namespace, "persistentvolumeclaim": claim}, gib * GI)
    return out
