"""Cluster-wide cost allocation: who spends what, what sits idle and what is wasted. $0, no LLM.

Right-sizing (pipeline.py) answers "what should this namespace request?". This module answers the question a
finance team asks first: "where does the node bill go?". It reads the same Prometheus/VictoriaMetrics, for
every namespace at once, and follows the OpenCost convention so the numbers can be checked against it:

- a pod is billed for ``max(requested, used)`` CPU and memory, per day, at the cluster's blended price per
  core-hour and GiB-hour (the node bill split between CPU and RAM by ``cpu_to_gib_cost_ratio``);
- whatever the nodes cost beyond that is **idle**: capacity that is paid for and that nobody asked for;
- provisioned volumes are billed per GiB-month.

Pods roll up to their controller (Deployment, StatefulSet, CronJob...), their namespace and their team. Shared
namespaces and idle capacity can be spread over the rest in proportion to spend, which gives a "with shared"
cost that always adds up to the whole bill. On top of the allocation come budgets, a daily trend, and findings
that right-sizing alone does not see: abandoned workloads, volumes nothing mounts, pods without requests, idle
node capacity and cost spikes.

`fetch` does the I/O and returns plain arrays; `build_report` is pure, so it is tested without a database.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
from pydantic import BaseModel

from kubesage.config import CostsConfig, SkuCatalog
from kubesage.metrics import owners
from kubesage.metrics.client import PromClient, PromError

DAY = 86400
GIB = 2**30
DAYS_PER_MONTH = 365 / 12
RUN_RATE_DAYS = 7  # the monthly run rate is the average of the last seven observed days
SHARED = "shared services"  # the team row that holds the shared namespaces
UNASSIGNED = "unassigned"
IDLE_WORKLOAD_RATIO = 0.015  # using under 1.5% of the requested CPU for days: nobody is calling it
OVER_REQUESTED_EFFICIENCY = 0.4
CPU_HEADROOM, MEM_HEADROOM = 1.5, 1.25  # what a right-sized request keeps above average use, for the estimate


# ---------- raw data ----------

@dataclass
class PodUsage:
    """One pod, per day of the window. CPU in core-hours, memory in GiB-hours."""
    cpu_used_h: np.ndarray
    cpu_req_h: np.ndarray
    mem_used_h: np.ndarray
    mem_req_h: np.ndarray


@dataclass
class Node:
    name: str
    hours: np.ndarray  # hours the node existed, per day
    sku: str | None = None  # from the node's SKU or instance-type label
    cpu: float | None = None  # capacity, cores
    mem_gib: float | None = None


@dataclass
class Volume:
    namespace: str
    name: str
    gib_h: np.ndarray  # provisioned GiB-hours per day
    pods: set[str] = field(default_factory=set)  # pods that mounted it during the window


@dataclass
class RawCosts:
    days: int
    end: float  # the window's last day ends here (epoch seconds)
    pods: dict[tuple[str, str], PodUsage] = field(default_factory=dict)  # (namespace, pod)
    owners: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)  # (namespace, pod) -> (kind, name)
    rs_owners: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)  # (namespace, replicaset)
    job_owners: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)  # (namespace, job)
    pod_teams: dict[tuple[str, str], str] = field(default_factory=dict)
    namespace_teams: dict[str, str] = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _label(name: str) -> str:
    """A Kubernetes label key as kube-state-metrics exports it: kubesage.io/sku -> label_kubesage_io_sku."""
    return "label_" + re.sub(r"[^a-zA-Z0-9_]", "_", name)


def fetch(client: PromClient, *, days: int, end: float, cfg: CostsConfig, catalog: SkuCatalog,
          resolution: int = 300) -> RawCosts:
    """Read `days` whole days ending at `end`. Seven range queries at a one-day step plus the metadata lookups."""
    end = int(end) // resolution * resolution
    start = end - (days - 1) * DAY
    raw = RawCosts(days=days, end=end)
    sel = 'container!="", container!="POD"'
    scale = resolution / 3600  # one subquery sample stands for this many hours
    sub, win = f"[1d:{resolution}s]", f"[{days}d]"

    def daily(query: str, keys: tuple[str, ...]) -> dict[tuple[str, ...], np.ndarray]:
        out: dict[tuple[str, ...], np.ndarray] = {}
        for series in client.query_range(query, start, end, DAY):
            row = out.setdefault(tuple(series["metric"].get(k, "") for k in keys), np.zeros(days))
            for ts, value in series["values"]:
                i, v = round((float(ts) - start) / DAY), float(value)
                if 0 <= i < days and math.isfinite(v):
                    row[i] += v
        return out

    def meta(query: str, what: str) -> list[dict]:
        """Metadata is optional: a cluster that doesn't export it still gets its costs, with a note."""
        try:
            return client.query(query, at=end)
        except PromError as exc:
            raw.notes.append(f"Could not read {what}: {exc}")
            return []

    pod = ("namespace", "pod")
    cpu_used = daily(f"sum by (namespace, pod) (increase(container_cpu_usage_seconds_total{{{sel}}}[1d])) / 3600", pod)
    mem_used = daily(f"sum by (namespace, pod) (sum_over_time(container_memory_working_set_bytes{{{sel}}}{sub})) "
                     f"* {scale} / {GIB}", pod)
    cpu_req = daily(f'sum by (namespace, pod) (sum_over_time(kube_pod_container_resource_requests{{resource="cpu", {sel}}}{sub})) '
                    f"* {scale}", pod)
    mem_req = daily(f'sum by (namespace, pod) (sum_over_time(kube_pod_container_resource_requests{{resource="memory", {sel}}}{sub})) '
                    f"* {scale} / {GIB}", pod)
    zero = np.zeros(days)
    for key in sorted(set(cpu_used) | set(mem_used) | set(cpu_req) | set(mem_req)):
        if key[0] and key[1]:
            raw.pods[key] = PodUsage(cpu_used.get(key, zero), cpu_req.get(key, zero), mem_used.get(key, zero),
                                     mem_req.get(key, zero))

    found = owners.fetch(client, at=end, days=days)
    raw.owners, raw.rs_owners, raw.job_owners = found.pods, found.replicasets, found.jobs
    raw.notes += found.notes

    team = _label(cfg.team_label)
    for r in meta(f'max by (namespace, pod, {team}) (last_over_time(kube_pod_labels{{{team}!=""}}{win}))', "pod labels"):
        raw.pod_teams[(r["metric"].get("namespace", ""), r["metric"].get("pod", ""))] = r["metric"].get(team, "")
    for r in meta(f'max by (namespace, {team}) (last_over_time(kube_namespace_labels{{{team}!=""}}{win}))', "namespace labels"):
        raw.namespace_teams[r["metric"].get("namespace", "")] = r["metric"].get(team, "")

    sku_labels = (_label(catalog.node_label), "label_node_kubernetes_io_instance_type")
    skus, capacity = {}, {}
    for r in meta(f"max by (node, {', '.join(sku_labels)}) (last_over_time(kube_node_labels{win}))", "node labels"):
        skus[r["metric"].get("node", "")] = next((r["metric"][k] for k in sku_labels if r["metric"].get(k)), None)
    for r in meta(f'max by (node, resource) (last_over_time(kube_node_status_capacity{{resource=~"cpu|memory"}}{win}))',
                  "node capacity"):
        capacity.setdefault(r["metric"].get("node", ""), {})[r["metric"].get("resource")] = float(r["value"][1])
    for (name,), hours in sorted(daily(f"sum by (node) (sum_over_time(kube_node_info{sub})) * {scale}", ("node",)).items()):
        cap = capacity.get(name, {})
        raw.nodes.append(Node(name=name, hours=np.minimum(hours, 24.0), sku=skus.get(name), cpu=cap.get("cpu"),
                              mem_gib=cap["memory"] / GIB if "memory" in cap else None))

    mounts: dict[tuple[str, str], set[str]] = {}
    for r in meta("max by (namespace, persistentvolumeclaim, pod) "
                  f"(last_over_time(kube_pod_spec_volumes_persistentvolumeclaims_info{win}))", "volume mounts"):
        m = r["metric"]
        mounts.setdefault((m.get("namespace", ""), m.get("persistentvolumeclaim", "")), set()).add(m.get("pod", ""))
    try:
        claims = daily("sum by (namespace, persistentvolumeclaim) "
                       f"(sum_over_time(kube_persistentvolumeclaim_resource_requests_storage_bytes{sub})) * {scale} / {GIB}",
                       ("namespace", "persistentvolumeclaim"))
    except PromError as exc:
        claims = {}
        raw.notes.append(f"Could not read volume claims: {exc}")
    for (namespace, name), gib_h in sorted(claims.items()):
        raw.volumes.append(Volume(namespace=namespace, name=name, gib_h=gib_h, pods=mounts.get((namespace, name), set())))
    return raw


# ---------- report ----------

class CostLine(BaseModel):
    """One row of a breakdown. Dollar fields ending in `_usd` cover the window; `monthly_*` is the run rate."""
    name: str
    namespace: str | None = None
    kind: str | None = None  # controller kind, for workload rows
    team: str | None = None
    shared: bool = False  # a shared namespace: its cost is spread over the others in `monthly_loaded_usd`
    pods: int = 0
    cpu_usd: float = 0.0
    ram_usd: float = 0.0
    storage_usd: float = 0.0
    total_usd: float = 0.0
    monthly_usd: float = 0.0  # direct cost as a monthly run rate
    monthly_loaded_usd: float = 0.0  # direct + its share of shared namespaces and idle capacity
    share_pct: float = 0.0  # of the cluster's monthly cost, loaded
    cpu_request_cores: float = 0.0  # averages over the observed part of the window
    cpu_used_cores: float = 0.0
    mem_request_gib: float = 0.0
    mem_used_gib: float = 0.0
    efficiency: float | None = None  # cost of what was used / cost of what was billed; None when nothing was billed
    daily_usd: list[float] = []


class NodeLine(BaseModel):
    name: str
    sku: str
    priced_by: str  # label | capacity | unit-price | assumed | config
    hourly_usd: float
    hours: float
    usd: float


class ClusterCosts(BaseModel):
    total_usd: float  # nodes + volumes over the window
    monthly_usd: float
    nodes_monthly_usd: float
    storage_monthly_usd: float
    allocated_monthly_usd: float
    idle_monthly_usd: float
    idle_pct: float
    efficiency: float | None
    cpu_capacity_cores: float
    cpu_request_cores: float
    cpu_used_cores: float
    mem_capacity_gib: float
    mem_request_gib: float
    mem_used_gib: float
    core_hour_usd: float
    gib_hour_usd: float
    daily_usd: list[float]
    daily_idle_usd: list[float]


class BudgetStatus(BaseModel):
    scope: str
    name: str
    budget_usd: float
    monthly_usd: float | None  # None: nothing with that name ran in the window
    used_pct: float | None
    status: str  # ok | at-risk | over | unknown


class Finding(BaseModel):
    kind: str  # idle-workload | over-requested | unused-volume | no-requests | idle-capacity | cost-spike
    severity: str  # saving | risk | info
    target: str
    title: str
    detail: str
    action: str
    monthly_usd: float = 0.0
    counted: bool = True  # False: informational, not part of potential_monthly_usd


class CostReport(BaseModel):
    generated_at: str
    window_days: int
    observed_days: int
    days: list[str]  # the local date each daily value belongs to
    cluster: ClusterCosts
    nodes: list[NodeLine]
    by_namespace: list[CostLine]
    by_workload: list[CostLine]
    by_team: list[CostLine]
    budgets: list[BudgetStatus]
    findings: list[Finding]
    potential_monthly_usd: float
    notes: list[str]


def workload_of(namespace: str, pod: str, raw: RawCosts) -> tuple[str, str]:
    """(kind, name) of the controller that owns a pod: the owner references kube-state-metrics exports, or the
    pod's name when there are none (see metrics/owners.py)."""
    return owners.resolve(namespace, pod, raw.owners, raw.rs_owners, raw.job_owners)


def _price_nodes(raw: RawCosts, catalog: SkuCatalog, observed: np.ndarray):
    """Per-day node cost split into CPU and RAM, the capacity behind it, and one NodeLine per node."""
    core_h, gib_h = catalog.unit_prices()
    by_name = {s.name.lower(): s for s in catalog.skus}
    priced: list[tuple[Node, str, str, float, float, float]] = []  # node, sku label, priced_by, hourly, vcpu, mem
    notes: list[str] = []
    nodes = raw.nodes
    if not nodes:
        pool = catalog.get(catalog.current_pool.sku)
        nodes = [Node(name=f"{pool.name}-{i + 1}", hours=np.where(observed, 24.0, 0.0), sku=pool.name)
                 for i in range(catalog.current_pool.nodes)]
        notes.append(f"No node metrics (kube_node_info) in the window: priced as the configured pool, "
                     f"{catalog.current_pool.nodes}× {pool.name} from config/skus.yaml.")
        source = "config"
    else:
        source = "label"
    for node in nodes:
        sku = by_name.get((node.sku or "").lower().removeprefix("standard_"))
        if sku is not None:
            priced.append((node, sku.name, source, sku.hourly_usd, float(sku.vcpu), float(sku.memory_gib)))
            continue
        if node.cpu and node.mem_gib:
            match = [s for s in catalog.skus if s.vcpu == round(node.cpu) and abs(s.memory_gib - node.mem_gib) <= 0.2 * s.memory_gib]
            if match:
                # Several SKUs can share a size; a burstable one is only assumed when nothing else fits.
                best = min(match, key=lambda s: (s.burstable, abs(s.memory_gib - node.mem_gib), s.hourly_usd))
                priced.append((node, best.name, "capacity", best.hourly_usd, float(best.vcpu), float(best.memory_gib)))
            else:
                hourly = node.cpu * core_h + node.mem_gib * gib_h
                priced.append((node, node.sku or f"{node.cpu:g} vCPU / {node.mem_gib:.0f} GiB", "unit-price", hourly,
                               node.cpu, node.mem_gib))
            continue
        ref = catalog.get(catalog.pricing_reference)
        priced.append((node, node.sku or ref.name, "assumed", ref.hourly_usd, float(ref.vcpu), float(ref.memory_gib)))
        notes.append(f"Node {node.name} has no known SKU label and no capacity metrics: priced as {ref.name}.")
    unlabelled = sorted({how for _, _, how, *_ in priced if how in ("capacity", "unit-price")})
    if unlabelled:
        notes.append("Some nodes carry no SKU label kube-state-metrics exports (start it with "
                     "--metric-labels-allowlist=nodes=[*]): they are priced from their capacity.")

    days = raw.days
    cpu_cost, mem_cost, cpu_cap, mem_cap = (np.zeros(days) for _ in range(4))
    lines = []
    for node, label, how, hourly, vcpu, mem in priced:
        cpu_share = vcpu / (vcpu + mem / catalog.cpu_to_gib_cost_ratio)
        cpu_cost += hourly * node.hours * cpu_share
        mem_cost += hourly * node.hours * (1 - cpu_share)
        cpu_cap += vcpu * node.hours
        mem_cap += mem * node.hours
        lines.append(NodeLine(name=node.name, sku=label, priced_by=how, hourly_usd=round(hourly, 4),
                              hours=round(float(node.hours.sum()), 1), usd=round(float((hourly * node.hours).sum()), 2)))
    return cpu_cost, mem_cost, cpu_cap, mem_cap, lines, notes


class _Acc:
    """Daily sums for one row."""
    __slots__ = ("cpu", "ram", "storage", "used", "cpu_req_h", "cpu_used_h", "mem_req_h", "mem_used_h", "pods",
                 "namespace", "kind", "team", "shared")

    def __init__(self, days: int, *, namespace=None, kind=None, team=None, shared=False):
        self.cpu, self.ram, self.storage, self.used = (np.zeros(days) for _ in range(4))
        self.cpu_req_h, self.cpu_used_h, self.mem_req_h, self.mem_used_h = (np.zeros(days) for _ in range(4))
        self.pods: set[tuple[str, str]] = set()
        self.namespace, self.kind, self.team, self.shared = namespace, kind, team, shared

    @property
    def direct(self) -> np.ndarray:
        return self.cpu + self.ram + self.storage


def _run_rate(daily: np.ndarray, recent: np.ndarray) -> float:
    """Monthly run rate: the mean of the recent observed days, times the days in an average month."""
    return float(daily[recent].mean()) * DAYS_PER_MONTH if recent.any() else 0.0


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def build_report(raw: RawCosts, cfg: CostsConfig, catalog: SkuCatalog, *, tz: str = "UTC") -> CostReport:
    days = raw.days
    notes = list(raw.notes)
    zone = ZoneInfo(tz)
    # Each value is a 24-hour bucket ending at end - k days; it is labelled with the local date of its midpoint.
    labels = [datetime.fromtimestamp(raw.end - (days - 1 - i) * DAY - DAY / 2, zone).date().isoformat() for i in range(days)]

    pod_seen = np.zeros(days, dtype=bool)
    for p in raw.pods.values():
        pod_seen |= (p.cpu_used_h + p.cpu_req_h + p.mem_used_h + p.mem_req_h) > 0
    node_seen = np.zeros(days, dtype=bool)
    for n in raw.nodes:
        node_seen |= n.hours > 0
    observed = node_seen | pod_seen
    cpu_cost, mem_cost, cpu_cap, mem_cap, node_lines, node_notes = _price_nodes(raw, catalog, observed)
    notes += node_notes
    observed = observed | (cpu_cap > 0)
    n_observed = int(observed.sum())
    recent = np.zeros(days, dtype=bool)
    recent[np.flatnonzero(observed)[-RUN_RATE_DAYS:]] = True
    observed_hours = max(n_observed * 24.0, 1.0)

    core_ref, gib_ref = catalog.unit_prices()
    with np.errstate(divide="ignore", invalid="ignore"):
        core_price = np.where(cpu_cap > 0, cpu_cost / cpu_cap, core_ref)
        gib_price = np.where(mem_cap > 0, mem_cost / mem_cap, gib_ref)
    storage_price = cfg.storage_usd_per_gib_month / catalog.hours_per_month  # per GiB-hour

    shared_ns = set(cfg.shared_namespaces)
    ns_acc: dict[str, _Acc] = {}
    wl_acc: dict[tuple[str, str, str], _Acc] = {}
    team_acc: dict[str, _Acc] = {}

    def team_of(namespace: str, pod: str | None) -> str:
        return ((raw.pod_teams.get((namespace, pod)) if pod else None) or raw.namespace_teams.get(namespace)
                or cfg.namespace_teams.get(namespace) or UNASSIGNED)

    def rows(namespace: str, kind: str, name: str, team: str) -> tuple[_Acc, _Acc, _Acc]:
        shared = namespace in shared_ns
        ns = ns_acc.setdefault(namespace, _Acc(days, namespace=namespace, shared=shared,
                                               team=raw.namespace_teams.get(namespace) or cfg.namespace_teams.get(namespace)))
        wl = wl_acc.setdefault((namespace, kind, name), _Acc(days, namespace=namespace, kind=kind, team=team, shared=shared))
        tm = team_acc.setdefault(SHARED if shared else team, _Acc(days, team=SHARED if shared else team, shared=shared))
        return ns, wl, tm

    pod_workload: dict[tuple[str, str], tuple[str, str]] = {}
    for (namespace, pod), p in raw.pods.items():
        kind, name = workload_of(namespace, pod, raw)
        pod_workload[(namespace, pod)] = (kind, name)
        cpu = np.maximum(p.cpu_req_h, p.cpu_used_h) * core_price
        ram = np.maximum(p.mem_req_h, p.mem_used_h) * gib_price
        used = p.cpu_used_h * core_price + p.mem_used_h * gib_price
        for acc in rows(namespace, kind, name, team_of(namespace, pod)):
            acc.cpu += cpu
            acc.ram += ram
            acc.used += used
            acc.cpu_req_h += p.cpu_req_h
            acc.cpu_used_h += p.cpu_used_h
            acc.mem_req_h += p.mem_req_h
            acc.mem_used_h += p.mem_used_h
            acc.pods.add((namespace, pod))

    unused_volumes: list[tuple[Volume, np.ndarray]] = []
    for v in raw.volumes:
        cost = v.gib_h * storage_price
        owners = sorted({pod_workload[(v.namespace, pod)] for pod in v.pods if (v.namespace, pod) in pod_workload})
        kind, name = owners[0] if owners else ("Volume", v.name)
        pod = next((p for p in sorted(v.pods) if (v.namespace, p) in pod_workload), None)
        for acc in rows(v.namespace, kind, name, team_of(v.namespace, pod)):
            acc.storage += cost
        if not v.pods and v.gib_h[-1] > 0:
            unused_volumes.append((v, cost))

    cpu_billed = sum((a.cpu for a in ns_acc.values()), np.zeros(days))
    ram_billed = sum((a.ram for a in ns_acc.values()), np.zeros(days))
    allocated = cpu_billed + ram_billed
    storage = sum((a.storage for a in ns_acc.values()), np.zeros(days))
    node_cost = cpu_cost + mem_cost
    # Idle is counted per resource: spare memory does not make up for CPU that is requested beyond capacity.
    idle = np.maximum(cpu_cost - cpu_billed, 0.0) + np.maximum(mem_cost - ram_billed, 0.0)
    if (cpu_billed > cpu_cost * 1.001).any() or (ram_billed > mem_cost * 1.001).any():
        notes.append("On some days the pods were billed for more than the nodes cost: requests exceed capacity, "
                     "or part of the node inventory is missing from the metrics. The allocation then adds up to "
                     "more than the node bill.")
    cluster_daily = node_cost + storage

    def pool_for(accs) -> tuple[float, float]:
        """(monthly cost to spread, monthly cost it is spread over) for one breakdown."""
        spread = _run_rate(idle, recent) + sum(_run_rate(a.direct, recent) for a in accs if a.shared)
        return spread, sum(_run_rate(a.direct, recent) for a in accs if not a.shared)

    cluster_monthly = _run_rate(cluster_daily, recent)

    def line(name: str, acc: _Acc, spread: float, base: float) -> CostLine:
        monthly = _run_rate(acc.direct, recent)
        loaded = monthly if acc.shared or base <= 0 else monthly + spread * monthly / base
        billed = float((acc.cpu + acc.ram).sum())
        return CostLine(
            name=name, namespace=acc.namespace, kind=acc.kind, team=acc.team, shared=acc.shared, pods=len(acc.pods),
            cpu_usd=round(float(acc.cpu.sum()), 2), ram_usd=round(float(acc.ram.sum()), 2),
            storage_usd=round(float(acc.storage.sum()), 2), total_usd=round(float(acc.direct.sum()), 2),
            monthly_usd=round(monthly, 2), monthly_loaded_usd=round(loaded, 2),
            share_pct=round(loaded / cluster_monthly * 100, 1) if cluster_monthly > 0 and not acc.shared else 0.0,
            cpu_request_cores=round(float(acc.cpu_req_h.sum()) / observed_hours, 3),
            cpu_used_cores=round(float(acc.cpu_used_h.sum()) / observed_hours, 3),
            mem_request_gib=round(float(acc.mem_req_h.sum()) / observed_hours, 3),
            mem_used_gib=round(float(acc.mem_used_h.sum()) / observed_hours, 3),
            efficiency=round(min(float(acc.used.sum()) / billed, 1.0), 3) if billed > 0 else None,
            daily_usd=[round(float(x), 4) for x in acc.direct])

    def breakdown(accs: dict, name_of) -> list[CostLine]:
        spread, base = pool_for(accs.values())
        out = [line(name_of(key), acc, spread, base) for key, acc in accs.items()]
        return sorted(out, key=lambda l: (-l.monthly_usd, l.namespace or "", l.name))

    by_namespace = breakdown(ns_acc, lambda k: k)
    by_workload = breakdown(wl_acc, lambda k: k[2])
    by_team = breakdown(team_acc, lambda k: k)

    billed_total = float(allocated.sum())
    used_total = float(sum((a.used for a in ns_acc.values()), np.zeros(days)).sum())
    idle_monthly, nodes_monthly = _run_rate(idle, recent), _run_rate(node_cost, recent)
    cluster = ClusterCosts(
        total_usd=round(float(cluster_daily.sum()), 2), monthly_usd=round(cluster_monthly, 2),
        nodes_monthly_usd=round(nodes_monthly, 2), storage_monthly_usd=round(_run_rate(storage, recent), 2),
        allocated_monthly_usd=round(_run_rate(allocated, recent), 2), idle_monthly_usd=round(idle_monthly, 2),
        idle_pct=round(idle_monthly / nodes_monthly * 100, 1) if nodes_monthly > 0 else 0.0,
        efficiency=round(min(used_total / billed_total, 1.0), 3) if billed_total > 0 else None,
        cpu_capacity_cores=round(float(cpu_cap.sum()) / observed_hours, 2),
        cpu_request_cores=round(sum(float(a.cpu_req_h.sum()) for a in ns_acc.values()) / observed_hours, 2),
        cpu_used_cores=round(sum(float(a.cpu_used_h.sum()) for a in ns_acc.values()) / observed_hours, 2),
        mem_capacity_gib=round(float(mem_cap.sum()) / observed_hours, 1),
        mem_request_gib=round(sum(float(a.mem_req_h.sum()) for a in ns_acc.values()) / observed_hours, 1),
        mem_used_gib=round(sum(float(a.mem_used_h.sum()) for a in ns_acc.values()) / observed_hours, 1),
        core_hour_usd=round(float(core_price[recent].mean()) if recent.any() else core_ref, 5),
        gib_hour_usd=round(float(gib_price[recent].mean()) if recent.any() else gib_ref, 5),
        daily_usd=[round(float(x), 4) for x in cluster_daily], daily_idle_usd=[round(float(x), 4) for x in idle])

    budgets = _budgets(cfg, cluster, by_namespace, by_team)
    findings = _findings(cfg, cluster, wl_acc, ns_acc, unused_volumes, recent, observed, core_price, gib_price, labels)
    if n_observed < days:
        notes.append(f"Only {n_observed} of the {days} days have data; monthly figures are a run rate from the days seen.")
    notes.append("Monthly figures are the last seven observed days as a run rate, at list prices from config/skus.yaml. "
                 "A pod is billed for the larger of what it requested and what it used.")
    if any(f.kind == "over-requested" for f in findings):
        notes.append(f"An over-requested workload's saving is an estimate that keeps {CPU_HEADROOM:g}× its average CPU and "
                     f"{MEM_HEADROOM:g}× its average memory. Savings reach the bill only when nodes are removed.")
    return CostReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), window_days=days, observed_days=n_observed,
        days=labels, cluster=cluster, nodes=node_lines, by_namespace=by_namespace, by_workload=by_workload,
        by_team=by_team, budgets=budgets, findings=findings,
        potential_monthly_usd=round(sum(f.monthly_usd for f in findings if f.counted), 2), notes=notes)


def _budgets(cfg: CostsConfig, cluster: ClusterCosts, by_namespace: list[CostLine],
             by_team: list[CostLine]) -> list[BudgetStatus]:
    out = []
    for b in cfg.budgets:
        if b.scope == "cluster":
            monthly: float | None = cluster.monthly_usd
        else:
            rows = by_namespace if b.scope == "namespace" else by_team
            monthly = next((l.monthly_usd for l in rows if l.name == b.name), None)
        if monthly is None:
            out.append(BudgetStatus(scope=b.scope, name=b.name, budget_usd=b.monthly_usd, monthly_usd=None,
                                    used_pct=None, status="unknown"))
            continue
        ratio = monthly / b.monthly_usd
        status = "over" if ratio > 1 else "at-risk" if ratio >= cfg.budget_warn_ratio else "ok"
        out.append(BudgetStatus(scope=b.scope, name=b.name or "cluster", budget_usd=b.monthly_usd,
                                monthly_usd=round(monthly, 2), used_pct=round(ratio * 100, 1), status=status))
    order = {"over": 0, "at-risk": 1, "unknown": 2, "ok": 3}
    return sorted(out, key=lambda b: (order[b.status], -(b.used_pct or 0)))


def _findings(cfg: CostsConfig, cluster: ClusterCosts, wl_acc: dict, ns_acc: dict, unused_volumes: list,
              recent: np.ndarray, observed: np.ndarray, core_price: np.ndarray, gib_price: np.ndarray,
              labels: list[str]) -> list[Finding]:
    out: list[Finding] = []
    n_recent = max(int(recent.sum()), 1)
    recent_hours = n_recent * 24.0

    for (namespace, kind, name), a in wl_acc.items():
        if kind == "Volume":
            continue
        req, used = float(a.cpu_req_h[recent].sum()), float(a.cpu_used_h[recent].sum())
        mem_req, mem_used = float(a.mem_req_h[recent].sum()), float(a.mem_used_h[recent].sum())
        monthly = _run_rate(a.direct, recent)
        target = f"{namespace}/{name}"
        running = (a.cpu_req_h + a.cpu_used_h + a.mem_used_h) > 0
        if not running[np.flatnonzero(observed)[-1:]].any():
            continue  # it is already gone
        if (used > 0 or mem_used > 0) and (req == 0 or mem_req == 0):
            missing = " and ".join(r for r, v in (("CPU", req), ("memory", mem_req)) if v == 0)
            out.append(Finding(
                kind="no-requests", severity="risk", target=target, counted=False,
                title=f"{name} runs without {missing} requests",
                detail=f"It uses {used / recent_hours:.2f} cores and {mem_used / recent_hours:.2f} GiB on average, but the "
                       f"scheduler reserves nothing for it, so it is billed by use alone and is first to be evicted.",
                action="Set requests on its containers; `kubesage analyze` sizes them from the measured use."))
            continue
        if a.shared or req == 0:
            continue
        if used / req < IDLE_WORKLOAD_RATIO and int(running[observed].sum()) >= 3:
            if monthly >= cfg.min_finding_usd:
                out.append(Finding(
                    kind="idle-workload", severity="saving", target=target, monthly_usd=round(monthly, 2),
                    title=f"{name} looks abandoned",
                    detail=f"It reserves {req / recent_hours:.2f} cores and {mem_req / recent_hours:.2f} GiB and has used "
                           f"{used / req:.1%} of that CPU over the last {n_recent} days.",
                    action=f"Ask {a.team or 'its owner'} whether it is still needed; scale it to zero or delete it."))
            continue
        billed = float((a.cpu + a.ram)[recent].sum())
        if billed > 0 and float(a.used[recent].sum()) / billed < OVER_REQUESTED_EFFICIENCY:
            spare = (np.maximum(a.cpu_req_h - CPU_HEADROOM * a.cpu_used_h, 0) * core_price
                     + np.maximum(a.mem_req_h - MEM_HEADROOM * a.mem_used_h, 0) * gib_price)
            saving = _run_rate(spare, recent)
            if saving >= cfg.min_finding_usd:
                out.append(Finding(
                    kind="over-requested", severity="saving", target=target, monthly_usd=round(saving, 2),
                    title=f"{name} requests far more than it uses on average",
                    detail=f"Requests {req / recent_hours:.2f} cores / {mem_req / recent_hours:.2f} GiB, uses "
                           f"{used / recent_hours:.2f} cores / {mem_used / recent_hours:.2f} GiB on average.",
                    action=f"Run `kubesage analyze --namespace {namespace}` for a right-sizing plan checked against "
                           f"its peaks."))

    for volume, cost in unused_volumes:
        monthly = _run_rate(cost, recent)
        if monthly >= cfg.min_finding_usd:
            out.append(Finding(
                kind="unused-volume", severity="saving", target=f"{volume.namespace}/{volume.name}",
                monthly_usd=round(monthly, 2), title=f"Volume {volume.name} is mounted by nothing",
                detail=f"{float(volume.gib_h[-1]) / 24:.0f} GiB provisioned in {volume.namespace}; no pod mounted it "
                       f"during the window.",
                action="Snapshot it if the data matters, then delete the claim."))

    if cluster.idle_pct >= cfg.idle_capacity_warn_pct and cluster.idle_monthly_usd >= cfg.min_finding_usd:
        out.append(Finding(
            kind="idle-capacity", severity="info", target="cluster", monthly_usd=cluster.idle_monthly_usd, counted=False,
            title=f"{cluster.idle_pct:.0f}% of the node bill is idle",
            detail=f"The nodes offer {cluster.cpu_capacity_cores:g} cores and {cluster.mem_capacity_gib:g} GiB; pods request "
                   f"{cluster.cpu_request_cores:g} cores and {cluster.mem_request_gib:g} GiB.",
            action="Savings above only reach the bill when nodes go: `kubesage analyze` plans the node pool for the "
                   "right-sized requests."))

    seen = np.flatnonzero(observed)
    if len(seen) >= 4:
        last, before = seen[-1], seen[-15:-1]
        for namespace, a in ns_acc.items():
            usual = statistics.median(float(x) for x in a.direct[before])
            today = float(a.direct[last])
            if today - usual >= cfg.spike_min_usd_per_day and today >= usual * (1 + cfg.spike_pct / 100):
                culprit, delta = None, 0.0
                for (ns, _kind, name), w in wl_acc.items():
                    if ns == namespace:
                        d = float(w.direct[last]) - statistics.median(float(x) for x in w.direct[before])
                        if d > delta:
                            culprit, delta = name, d
                pct = f"{(today / usual - 1) * 100:.0f}% above" if usual > 0 else "up from nothing against"
                out.append(Finding(
                    kind="cost-spike", severity="risk", target=namespace, counted=False,
                    monthly_usd=round((today - usual) * DAYS_PER_MONTH, 2),
                    title=f"{namespace} cost {_usd(today)} on {labels[last]}, {pct} its usual day",
                    detail=f"Its usual day is {_usd(usual)}." + (f" Most of the rise is {culprit} (+{_usd(delta)} a day)."
                                                               if culprit else ""),
                    action="Check whether the extra load is meant to stay; the figure is what it costs per month if it does."))

    order = {"saving": 0, "risk": 1, "info": 2}
    return sorted(out, key=lambda f: (order[f.severity], -f.monthly_usd, f.target))


def costs_now(*, vm: str, days: int, tz: str, end: float, cfg: CostsConfig, catalog: SkuCatalog,
              client: PromClient | None = None) -> CostReport:
    return build_report(fetch(client or PromClient(vm), days=days, end=end, cfg=cfg, catalog=catalog), cfg, catalog, tz=tz)


def prometheus_text(report: CostReport) -> str:
    """The report as Prometheus exposition text, so Grafana and Alertmanager can chart and alert on it."""
    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    out = ["# HELP kubesage_cluster_monthly_cost_usd Cluster cost (nodes and volumes) as a monthly run rate.",
           "# TYPE kubesage_cluster_monthly_cost_usd gauge", f"kubesage_cluster_monthly_cost_usd {report.cluster.monthly_usd}",
           "# HELP kubesage_cluster_idle_monthly_cost_usd Node cost nobody requested, as a monthly run rate.",
           "# TYPE kubesage_cluster_idle_monthly_cost_usd gauge", f"kubesage_cluster_idle_monthly_cost_usd {report.cluster.idle_monthly_usd}",
           "# HELP kubesage_potential_monthly_savings_usd Sum of the savings findings.",
           "# TYPE kubesage_potential_monthly_savings_usd gauge", f"kubesage_potential_monthly_savings_usd {report.potential_monthly_usd}",
           "# HELP kubesage_monthly_cost_usd Direct cost as a monthly run rate, by namespace or team.",
           "# TYPE kubesage_monthly_cost_usd gauge"]
    for scope, rows in (("namespace", report.by_namespace), ("team", report.by_team)):
        out += [f'kubesage_monthly_cost_usd{{scope="{scope}",name="{esc(l.name)}"}} {l.monthly_usd}' for l in rows]
    out += ["# HELP kubesage_efficiency_ratio Cost of what was used over cost of what was billed, by namespace.",
            "# TYPE kubesage_efficiency_ratio gauge"]
    out += [f'kubesage_efficiency_ratio{{namespace="{esc(l.name)}"}} {l.efficiency}' for l in report.by_namespace
            if l.efficiency is not None]
    out += ["# HELP kubesage_budget_used_ratio Monthly run rate over the budget; above 1 is over budget.",
            "# TYPE kubesage_budget_used_ratio gauge"]
    out += [f'kubesage_budget_used_ratio{{scope="{b.scope}",name="{esc(b.name)}"}} {round(b.used_pct / 100, 4)}'
            for b in report.budgets if b.used_pct is not None]
    return "\n".join(out) + "\n"
