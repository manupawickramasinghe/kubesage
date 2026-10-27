"""The 3-stage funnel: deterministic stats -> cheap triage -> mid/frontier tier only where needed."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from pydantic import BaseModel

from kubesage.config import SkuCatalog
from kubesage.llm import mock, prompts
from kubesage.llm.client import LedgerEntry, LLMClient
from kubesage.llm.schemas import Recommendation, SkuChoice, TriageItem, TriageReport
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import costing, guardrails, render

ROUTE = {"ROUTINE": "worker", "COMPLEX": "strategist"}
RAW_TOKENS_PER_POINT = 7  # "1761552000,0.1234\n" style CSV row, used only for the naive-baseline estimate
SUSTAINED_SIGNALS = {"batch-pattern", "recurring-peak"}


class WorkloadResult(BaseModel):
    id: str
    triage: TriageItem
    tier: str | None = None
    recommendation: Recommendation | None = None
    guardrail_notes: list[str] = []
    monthly_before_usd: float
    monthly_after_usd: float
    manifests: dict[str, str] = {}


class NodePoolResult(BaseModel):
    before_sku: str
    before_nodes: int
    before_monthly_usd: float
    choice: SkuChoice
    after_monthly_usd: float
    candidates: list[costing.SkuCandidate]
    demand_before: costing.Demand
    demand_after: costing.Demand
    notes: list[str] = []


class AnalysisResult(BaseModel):
    generated_at: str
    mode: str
    namespace: str
    window_days: int
    stats: list[WorkloadStats]
    workloads: list[WorkloadResult]
    node_pool: NodePoolResult | None
    ledger: list[LedgerEntry]
    notes: list[str]
    llm_cost_usd: float
    baselines_usd: dict[str, float]


def _compact(s: WorkloadStats) -> dict:
    return s.model_dump(exclude_none=True)


def _demand(rows: list[tuple[float, float, float]]) -> costing.Demand:
    """rows: (cpu cores per pod, MiB per pod, pods at peak)."""
    return costing.Demand(cpu_cores=round(sum(c * n for c, _, n in rows), 3),
                          mem_mib=round(sum(m * n for _, m, n in rows), 1),
                          largest_pod_cpu=max((c for c, _, _ in rows), default=0.0),
                          largest_pod_mem_mib=max((m for _, m, _ in rows), default=0.0))


def analyze(stats: list[WorkloadStats], *, llm: LLMClient, catalog: SkuCatalog, namespace: str, days: int,
            tz: str, raw_points: int) -> AnalysisResult:
    ledger: list[LedgerEntry] = []
    notes: list[str] = []

    # Stage 2: one cheap call triages every workload.
    report, entry = llm.structured("triage", prompts.triage_system(), json.dumps([_compact(s) for s in stats]),
                                   TriageReport, lambda: mock.triage(stats), stage="triage", workload="*")
    ledger.append(entry)
    by_id = {i.workload: i for i in report.items}
    triage: dict[str, TriageItem] = {}
    for s in stats:
        if s.id not in by_id:
            notes.append(f"triage omitted {s.id}; used deterministic triage")
        triage[s.id] = by_id.get(s.id) or mock.triage_one(s)

    # Stage 3: only non-healthy workloads reach a bigger model, in parallel.
    def remediate(s: WorkloadStats) -> tuple[WorkloadStats, Recommendation, LedgerEntry, str]:
        item, tier = triage[s.id], ROUTE[triage[s.id].status]
        user = json.dumps({"workload": _compact(s), "triage": item.model_dump()})
        rec, e = llm.structured(tier, prompts.recommend_system(tz), user, Recommendation,
                                lambda: mock.recommend(s, item), stage="recommend", workload=s.id)
        return s, rec, e, tier

    todo = [s for s in stats if triage[s.id].status in ROUTE]
    with ThreadPoolExecutor(max_workers=4) as pool:
        done = {s.id: (rec, e, tier) for s, rec, e, tier in pool.map(remediate, todo)}

    results, before_rows, after_rows = [], [], []
    for s in stats:
        cpu_now, mem_now = s.cpu_request_cores or 0.0, s.mem_request_mib or 0.0
        before = costing.allocation_monthly(cpu_now, mem_now, s.replicas, catalog)
        before_rows.append((cpu_now, mem_now, s.replicas))
        result = WorkloadResult(id=s.id, triage=triage[s.id], monthly_before_usd=round(before, 2),
                                monthly_after_usd=round(before, 2))
        if s.id in done:
            raw, e, tier = done[s.id]
            ledger.append(e)
            rec, gnotes = guardrails.apply(raw, s)
            cpu_new = rec.resources.cpu_request_m / 1000 if rec.resources else cpu_now
            mem_new = rec.resources.mem_request_mi if rec.resources else mem_now
            after = costing.allocation_monthly(cpu_new, mem_new, costing.average_replicas(rec.scaling, s.replicas), catalog)
            after_rows.append((cpu_new, mem_new, costing.peak_replicas(rec.scaling, s.replicas)))
            result = result.model_copy(update=dict(
                tier=tier, recommendation=rec, guardrail_notes=gnotes, monthly_after_usd=round(after, 2),
                manifests=render.render(rec, namespace=s.namespace, name=s.name, container=s.name, tz=tz)))
        else:
            after_rows.append((cpu_now, mem_now, s.replicas))
        results.append(result)

    node_pool = None
    if done:
        node_pool = _plan_nodes(llm, catalog, stats, _demand(before_rows), _demand(after_rows), ledger)

    llm_cost = sum(e.cost_usd for e in ledger)
    strategist = llm.models.tiers["strategist"]
    out_tokens = sum(e.output_tokens for e in ledger)
    baselines = {
        # Same calls, but every one of them on the frontier tier.
        "all_calls_on_strategist": round(sum(strategist.cost(e.input_tokens, e.cached_tokens, e.output_tokens)
                                             for e in ledger), 6),
        # No Stage 1 at all: dump the raw 5-minute series into the frontier model.
        "raw_series_to_strategist": round(strategist.cost(raw_points * RAW_TOKENS_PER_POINT, 0, out_tokens), 6),
    }
    return AnalysisResult(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), mode=llm.mode,
                          namespace=namespace, window_days=days, stats=stats, workloads=results, node_pool=node_pool,
                          ledger=ledger, notes=notes, llm_cost_usd=round(llm_cost, 6), baselines_usd=baselines)


def _plan_nodes(llm: LLMClient, catalog: SkuCatalog, stats: list[WorkloadStats], before: costing.Demand,
                after: costing.Demand, ledger: list[LedgerEntry]) -> NodePoolResult | None:
    candidates = costing.node_candidates(after, catalog)
    if not candidates:
        return None
    sustained = any(SUSTAINED_SIGNALS & set(s.signals) for s in stats)
    current = catalog.current_pool
    user = json.dumps({"current_pool": current.model_dump(), "demand_before": before.model_dump(),
                       "demand_after": after.model_dump(), "candidates": [c.model_dump() for c in candidates],
                       "sustained_load_workloads": [s.id for s in stats if SUSTAINED_SIGNALS & set(s.signals)]})
    choice, e = llm.structured("strategist", prompts.sku_system(catalog), user, SkuChoice,
                               lambda: mock.sku_choice(candidates, sustained), stage="node-pool", workload="cluster")
    ledger.append(e)
    notes = []
    match = next((c for c in candidates if c.sku == choice.sku), None)
    if match is None:
        notes.append(f"model picked {choice.sku!r}, not a valid candidate; fell back to deterministic choice")
        choice = mock.sku_choice(candidates, sustained)
        match = next(c for c in candidates if c.sku == choice.sku)
    if choice.nodes < match.nodes:
        notes.append(f"node count raised {choice.nodes} -> {match.nodes} to fit demand")
        choice = choice.model_copy(update={"nodes": match.nodes})
    return NodePoolResult(before_sku=current.sku, before_nodes=current.nodes,
                          before_monthly_usd=costing.pool_monthly(current.sku, current.nodes, catalog), choice=choice,
                          after_monthly_usd=costing.pool_monthly(choice.sku, choice.nodes, catalog),
                          candidates=candidates, demand_before=before, demand_after=after, notes=notes)
