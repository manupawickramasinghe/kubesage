"""Dollar math: allocation cost per workload (requests x unit price) and node-pool sizing per SKU."""
from __future__ import annotations

import math

from pydantic import BaseModel

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
    extra = sum((w.desired_replicas - plan.min_replicas) * cron.window_hours_per_week(w.start, w.end) / 168
                for w in plan.cron_windows)
    return plan.min_replicas + max(extra, 0.0)


def peak_replicas(plan: ScalingPlan | None, fixed: int) -> int:
    if plan is None:
        return fixed
    return max([plan.min_replicas, 1, *(w.desired_replicas for w in plan.cron_windows)])


class Demand(BaseModel):
    cpu_cores: float
    mem_mib: float
    largest_pod_cpu: float
    largest_pod_mem_mib: float


class SkuCandidate(BaseModel):
    sku: str
    family: str
    nodes: int
    monthly_usd: float


def node_candidates(demand: Demand, catalog: SkuCatalog) -> list[SkuCandidate]:
    """Every SKU that can host the demand, with the node count it needs, cheapest first."""
    out = []
    for sku in catalog.skus:
        cpu_alloc, mem_alloc = sku.vcpu * catalog.allocatable_cpu, sku.memory_gib * 1024 * catalog.allocatable_mem
        if demand.largest_pod_cpu > cpu_alloc or demand.largest_pod_mem_mib > mem_alloc:
            continue
        nodes = max(catalog.min_nodes, math.ceil(demand.cpu_cores / cpu_alloc), math.ceil(demand.mem_mib / mem_alloc))
        out.append(SkuCandidate(sku=sku.name, family=sku.family, nodes=nodes,
                                monthly_usd=round(nodes * sku.hourly_usd * catalog.hours_per_month, 2)))
    return sorted(out, key=lambda c: c.monthly_usd)


def pool_monthly(sku: str, nodes: int, catalog: SkuCatalog) -> float:
    return round(nodes * catalog.get(sku).hourly_usd * catalog.hours_per_month, 2)
