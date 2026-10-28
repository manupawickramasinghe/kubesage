"""Dollar math: allocation cost per workload (requests x unit price) and node-pool sizing per SKU."""
from __future__ import annotations

import math

from pydantic import BaseModel, Field

from kubesage.config import SkuCatalog
from kubesage.llm.schemas import ScalingPlan
from kubesage.recommend import cron


def allocation_monthly(cpu_cores: float, mem_mib: float, replicas: float, catalog: SkuCatalog) -> float:
    core_h, gib_h = catalog.unit_prices()
    return (cpu_cores * core_h + mem_mib / 1024 * gib_h) * replicas * catalog.hours_per_month


def average_replicas(plan: ScalingPlan | None, fixed: int) -> float:
    """Expected replica count over a week. CPU-triggered scale-out is not modelled (unknown in advance)."""
    if plan is None:
        return float(fixed)
    replicas = [plan.min_replicas] * cron.MINUTES_PER_WEEK
    for w in plan.cron_windows:
        # HPA uses the largest active trigger, rather than adding overlapping cron targets together.
        for minute in cron.active_minutes(w.start, w.end):
            replicas[minute] = max(replicas[minute], w.desired_replicas)
    return sum(replicas) / cron.MINUTES_PER_WEEK


def peak_replicas(plan: ScalingPlan | None, fixed: int) -> int:
    """Replicas at the scheduled peak: what the node pool is sized for."""
    if plan is None:
        return fixed
    return max([plan.min_replicas, 1, *(w.desired_replicas for w in plan.cron_windows)])


def burst_replicas(plan: ScalingPlan | None, fixed: int) -> int:
    """Replicas if a CPU autoscaler goes all the way to its ceiling. Checked against the pool, not sized for:
    sizing every workload for its maximum at once is the over-provisioning KubeSage removes."""
    if plan is not None and plan.cpu_utilization_target is not None:
        return max(plan.max_replicas, peak_replicas(plan, fixed))
    return peak_replicas(plan, fixed)


def allocatable(sku: str, nodes: int, catalog: SkuCatalog) -> tuple[float, float]:
    """(cores, MiB) that pods can request on `nodes` of `sku`."""
    s = catalog.get(sku)
    return nodes * s.vcpu * catalog.allocatable_cpu, nodes * s.memory_gib * 1024 * catalog.allocatable_mem


class Demand(BaseModel):
    cpu_cores: float
    mem_mib: float
    largest_pod_cpu: float
    largest_pod_mem_mib: float
    # Every pod's (cores, MiB), for packing; kept out of report.json and the model's prompt.
    pods: list[tuple[float, float]] = Field(default_factory=list, exclude=True)


def nodes_to_pack(pods: list[tuple[float, float]], cpu_alloc: float, mem_alloc: float) -> int | None:
    """Nodes first-fit-decreasing needs to place every pod, or None if one pod is bigger than a node. Totals alone
    say four 2-core pods fit three nodes of 3.6 allocatable cores; placed, they need four."""
    bins: list[list[float]] = []
    for cpu, mem in sorted(pods, key=lambda p: max(p[0] / cpu_alloc, p[1] / mem_alloc), reverse=True):
        if cpu > cpu_alloc or mem > mem_alloc:
            return None
        for b in bins:
            if b[0] + cpu <= cpu_alloc + 1e-9 and b[1] + mem <= mem_alloc + 1e-9:
                b[0] += cpu
                b[1] += mem
                break
        else:
            bins.append([cpu, mem])
    return len(bins)


class SkuCandidate(BaseModel):
    sku: str
    family: str
    nodes: int
    monthly_usd: float


def node_candidates(demand: Demand, catalog: SkuCatalog) -> list[SkuCandidate]:
    """Every SKU that can host the demand, with the node count it needs, cheapest first."""
    out = []
    for sku in (s for s in catalog.skus if s.planner):
        cpu_alloc, mem_alloc = sku.vcpu * catalog.allocatable_cpu, sku.memory_gib * 1024 * catalog.allocatable_mem
        if demand.largest_pod_cpu > cpu_alloc or demand.largest_pod_mem_mib > mem_alloc:
            continue
        packed = nodes_to_pack(demand.pods, cpu_alloc, mem_alloc) or 0
        nodes = max(catalog.min_nodes, math.ceil(demand.cpu_cores / cpu_alloc), math.ceil(demand.mem_mib / mem_alloc), packed)
        out.append(SkuCandidate(sku=sku.name, family=sku.family, nodes=nodes,
                                monthly_usd=round(nodes * sku.hourly_usd * catalog.hours_per_month, 2)))
    return sorted(out, key=lambda c: c.monthly_usd)


def pool_monthly(sku: str, nodes: int, catalog: SkuCatalog) -> float:
    return round(nodes * catalog.get(sku).hourly_usd * catalog.hours_per_month, 2)
