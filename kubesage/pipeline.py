"""The 3-stage funnel: deterministic stats -> cheap triage -> mid/frontier tier only where needed."""
from __future__ import annotations

import json
import math
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from kubesage.config import BusinessCalendar, SkuCatalog
from kubesage.forecast import WorkloadForecast, build_forecasts
from kubesage.journal import log
from kubesage.llm import mock, prompts
from kubesage.llm.client import LedgerEntry, LLMClient
from kubesage.llm.schemas import Recommendation, SkuChoice, TriageItem, TriageReport
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import costing, guardrails, render, timed

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
    monthly_savings_usd: float = 0.0
    monthly_savings_pct: float | None = None  # positive = cheaper; None when there was no baseline spend
    manifests: dict[str, str] = {}


class NodePoolResult(BaseModel):
    before_sku: str
    before_nodes: int
    before_monthly_usd: float
    choice: SkuChoice
    after_monthly_usd: float
    monthly_savings_usd: float = 0.0
    monthly_savings_pct: float | None = None
    candidates: list[costing.SkuCandidate]
    demand_before: costing.Demand
    demand_after: costing.Demand
    demand_burst: costing.Demand | None = None  # every CPU autoscaler at its maximum; checked, not sized for
    # Pods the analysis does not resize but the nodes must still hold: other namespaces, DaemonSets, sidecars.
    # They are part of every demand above, at their current requests.
    background_pods: int = 0
    background_cpu_cores: float = 0.0
    background_mem_mib: float = 0.0
    node_label: str
    migrations: dict[str, str] = {}  # filename -> nodeSelector patch, empty when the SKU doesn't change
    notes: list[str] = []


class AnalysisResult(BaseModel):
    generated_at: str
    mode: str
    namespace: str  # the analysed namespace; several are joined with commas
    namespaces: list[str] = []
    window_days: int
    tiers: dict[str, str] = {}  # tier key -> display label, so a report renders the same wherever it's opened
    stats: list[WorkloadStats]
    forecasts: list[WorkloadForecast] = []
    workloads: list[WorkloadResult]
    node_pool: NodePoolResult | None
    ledger: list[LedgerEntry]
    notes: list[str]
    llm_cost_usd: float
    baselines_usd: dict[str, float]


def write_run(result: AnalysisResult, run_dir: Path, trace: list[dict] | None = None) -> Path:
    """Publish a complete immutable report + manifests snapshot with an atomic directory rename.
    `trace` (the PromQL and LLM exchanges as sent) goes to trace.json, kept apart from the report it explains."""
    if run_dir.exists():
        raise FileExistsError(f"analysis run already exists: {run_dir}")
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".run-", dir=run_dir.parent))
    staging.chmod(run_dir.parent.stat().st_mode & 0o777)  # mkdtemp makes it 0700; a run is as readable as out/
    try:
        (staging / "manifests").mkdir()
        files = {name: text for w in result.workloads for name, text in w.manifests.items()}
        if result.node_pool:
            files.update(result.node_pool.migrations)
        for name, text in files.items():
            (staging / "manifests" / name).write_text(text)
        (staging / "report.json").write_text(result.model_dump_json(indent=2))
        if trace is not None:
            (staging / "trace.json").write_text(json.dumps(trace, indent=1, default=str))
        staging.rename(run_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return run_dir


def _compact(s: WorkloadStats) -> dict:
    """What a model is shown. The common shape (a Deployment, one container named like it) adds nothing."""
    out = s.model_dump(exclude_none=True)
    if s.kind == "Deployment":
        del out["kind"]
    if not s.sidecars:
        del out["sidecars"]
    return out


def _burst_note(burst: costing.Demand, pool: "NodePoolResult", catalog: SkuCatalog) -> str:
    """The pool is sized for scheduled peaks; say what happens if every CPU autoscaler maxes out at once."""
    cpu, mem = costing.allocatable(pool.choice.sku, pool.choice.nodes, catalog)
    per_node = costing.allocatable(pool.choice.sku, 1, catalog)
    packed = costing.nodes_to_pack(burst.pods, *per_node) if burst.pods else 0
    if burst.cpu_cores <= cpu and burst.mem_mib <= mem and packed is not None and packed <= pool.choice.nodes:
        return (f"Sized for scheduled peaks; it also fits every CPU autoscaler at its maximum "
                f"({burst.cpu_cores:.2f} of {cpu:.2f} allocatable cores).")
    if burst.cpu_cores <= cpu and burst.mem_mib <= mem:
        return (f"Sized for scheduled peaks. If every CPU autoscaler reached its maximum at once, the totals "
                f"({burst.cpu_cores:.2f} of {cpu:.2f} cores) fit but the pods don't pack onto {pool.choice.nodes} "
                f"{pool.choice.sku} node(s){f' (they need {packed})' if packed else ''}, so some scale-out pods would "
                f"stay Pending until load drops.")
    short = [f"{burst.cpu_cores - cpu:.2f} cores" if burst.cpu_cores > cpu else "",
             f"{(burst.mem_mib - mem) / 1024:.1f} GiB" if burst.mem_mib > mem else ""]
    return (f"Sized for scheduled peaks. If every CPU autoscaler reached its maximum at once, pods would request "
            f"{burst.cpu_cores:.2f} cores / {burst.mem_mib / 1024:.1f} GiB against {cpu:.2f} / {mem / 1024:.1f} allocatable: "
            f"{' and '.join(x for x in short if x)} short, so some scale-out pods would stay Pending until load drops.")


def _demand(rows: list[tuple[float, float, float]]) -> costing.Demand:
    """rows: (cpu cores per pod, MiB per pod, pods at peak)."""
    return costing.Demand(cpu_cores=round(sum(c * n for c, _, n in rows), 3),
                          mem_mib=round(sum(m * n for _, m, n in rows), 1),
                          largest_pod_cpu=max((c for c, _, _ in rows), default=0.0),
                          largest_pod_mem_mib=max((m for _, m, _ in rows), default=0.0),
                          pods=[(c, m) for c, m, n in rows for _ in range(math.ceil(n))])


def analyze(stats: list[WorkloadStats], *, llm: LLMClient, catalog: SkuCatalog, namespace: str, days: int,
            tz: str, raw_points: int, calendar: BusinessCalendar | None = None,
            background: list[tuple[float, float]] | None = None, notes: list[str] | None = None) -> AnalysisResult:
    """`background` is every pod the analysis leaves alone, as (CPU cores, MiB) requested: the node pool has to
    hold them too. `notes` are carried into the report (what the cluster inventory could not find)."""
    ledger: list[LedgerEntry] = []
    notes = list(notes or [])
    background = [(c, m) for c, m in (background or []) if c > 0 or m > 0]
    namespaces = sorted({s.namespace for s in stats})
    # One namespace: files are named after the workload, as always. Several: <namespace>.<name>, so two
    # namespaces' workloads of the same name don't overwrite each other.
    stem = (lambda s: f"{s.namespace}.{s.name}") if len(namespaces) > 1 else (lambda s: s.name)

    # Stage 1.5: deterministic forecast ($0, no LLM). History peaks plus the operator's business calendar
    # become expected load, pre-warm times and replica counts the models reason about but never invent.
    forecasts = build_forecasts(stats, calendar)
    forecast_by_id = {f.workload: f for f in forecasts}
    for f in forecasts:
        if f.windows:
            log("forecast", f.summary, stage=1)
    if calendar and calendar.events:
        notes.append(f"forecast used {len(calendar.events)} declared calendar event(s) from config/calendar.yaml; "
                     "the models were told not to invent or move events")

    # Stage 2: one cheap call triages every workload.
    report, entry = llm.structured("triage", prompts.triage_system(),
                                   json.dumps({"workloads": [_compact(s) for s in stats],
                                               "forecasts": [f.model_dump() for f in forecasts]}),
                                   TriageReport, lambda: mock.triage(stats), stage="triage", workload="*")
    ledger.append(entry)
    by_id = {i.workload: i for i in report.items}
    triage: dict[str, TriageItem] = {}
    for s in stats:
        if s.id not in by_id:
            notes.append(f"triage omitted {s.id}; used deterministic triage")
        triage[s.id] = by_id.get(s.id) or mock.triage_one(s)
        log("router", f"triage {s.id}: {triage[s.id].status}", stage=2)

    # Stage 3: only non-healthy workloads reach a bigger model, in parallel.
    def remediate(s: WorkloadStats) -> tuple[WorkloadStats, Recommendation, LedgerEntry, str]:
        item, tier = triage[s.id], ROUTE[triage[s.id].status]
        forecast = forecast_by_id[s.id]
        user = json.dumps({"workload": _compact(s), "triage": item.model_dump(),
                           "forecast": forecast.model_dump()})
        rec, e = llm.structured(tier, prompts.recommend_system(tz), user, Recommendation,
                                lambda: mock.recommend(s, item, forecast), stage="recommend", workload=s.id)
        return s, rec, e, tier

    todo = [s for s in stats if triage[s.id].status in ROUTE]
    with ThreadPoolExecutor(max_workers=4) as pool:
        done = {s.id: (rec, e, tier) for s, rec, e, tier in pool.map(remediate, todo)}

    results, before_rows, after_rows, burst_rows = [], [], [], []
    for s in stats:
        cpu_now, mem_now = s.cpu_request_cores or 0.0, s.mem_request_mib or 0.0
        before = costing.allocation_monthly(cpu_now, mem_now, s.replicas, catalog)
        before_rows.append((cpu_now, mem_now, s.replicas))
        result = WorkloadResult(id=s.id, triage=triage[s.id], monthly_before_usd=round(before, 2),
                                monthly_after_usd=round(before, 2),
                                monthly_savings_pct=costing.savings_pct(before, before))
        if s.id in done:
            raw, e, tier = done[s.id]
            ledger.append(e)
            rec, gnotes = guardrails.apply(raw, s, forecast_by_id[s.id])
            for note in gnotes:
                log("guardrails", f"{s.id}: {note}", stage=3)
            cpu_new = rec.resources.cpu_request_m / 1000 if rec.resources else cpu_now
            mem_new = rec.resources.mem_request_mi if rec.resources else mem_now
            after = costing.allocation_monthly(cpu_new, mem_new, costing.average_replicas(rec.scaling, s.replicas), catalog)
            after_rows.append((cpu_new, mem_new, costing.peak_replicas(rec.scaling, s.replicas)))
            burst_rows.append((cpu_new, mem_new, costing.burst_replicas(rec.scaling, s.replicas)))
            # Dated calendar events (Black Friday) are not the model's to schedule: they join the ScaledObject as
            # cron triggers with a date, in the same run, or the forecast's promise is never applied.
            manifests = render.render(rec, namespace=s.namespace, name=s.name, container=s.container or s.name, tz=tz,
                                      dated=timed.dated_triggers(forecast_by_id[s.id]), current_replicas=s.replicas,
                                      kind=s.kind, stem=stem(s))
            result = result.model_copy(update=dict(
                tier=tier, recommendation=rec, guardrail_notes=gnotes, monthly_after_usd=round(after, 2),
                monthly_savings_usd=round(before - after, 2),
                monthly_savings_pct=costing.savings_pct(before, after),
                manifests=manifests))
        else:
            after_rows.append((cpu_now, mem_now, s.replicas))
            burst_rows.append((cpu_now, mem_now, s.replicas))
        results.append(result)

    node_pool = None
    if done:
        fixed = [(c, m, 1) for c, m in background]  # unchanged before and after, but the nodes must hold them
        node_pool = _plan_nodes(llm, catalog, stats, _demand(before_rows + fixed), _demand(after_rows + fixed), ledger, stem)
        burst = _demand(burst_rows + fixed)
        if node_pool and background:
            node_pool.background_pods = len(background)
            node_pool.background_cpu_cores = round(sum(c for c, _ in background), 3)
            node_pool.background_mem_mib = round(sum(m for _, m in background), 1)
            node_pool.notes.append(
                f"Counts {len(background)} pod(s) this analysis does not resize (other namespaces, DaemonSets, sidecars) "
                f"at their current requests: {node_pool.background_cpu_cores:g} cores, "
                f"{node_pool.background_mem_mib / 1024:.1f} GiB.")
        if node_pool and (burst.cpu_cores, burst.mem_mib) != (node_pool.demand_after.cpu_cores, node_pool.demand_after.mem_mib):
            node_pool.demand_burst = burst
            node_pool.notes.append(_burst_note(burst, node_pool, catalog))
        if node_pool:
            log("planner", f"node pool {node_pool.before_nodes}x {node_pool.before_sku} ${node_pool.before_monthly_usd:.2f}/mo -> "
                           f"{node_pool.choice.nodes}x {node_pool.choice.sku} ${node_pool.after_monthly_usd:.2f}/mo, "
                           f"{len(node_pool.migrations)} migrations", stage=3)

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
                          namespace=namespace, namespaces=namespaces, window_days=days, stats=stats, forecasts=forecasts,
                          tiers={name: t.label or name for name, t in llm.models.tiers.items()},
                          workloads=results, node_pool=node_pool,
                          ledger=ledger, notes=notes, llm_cost_usd=round(llm_cost, 6), baselines_usd=baselines)


def _plan_nodes(llm: LLMClient, catalog: SkuCatalog, stats: list[WorkloadStats], before: costing.Demand,
                after: costing.Demand, ledger: list[LedgerEntry], stem=lambda s: s.name) -> NodePoolResult | None:
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
    migrations = {}
    if choice.sku != current.sku:
        migrations = {f"{stem(s)}.migrate.yaml": render.migration(s.namespace, s.name, catalog.node_label, choice.sku, s.kind)
                      for s in stats}
    before_usd = costing.pool_monthly(current.sku, current.nodes, catalog)
    after_usd = costing.pool_monthly(choice.sku, choice.nodes, catalog)
    pct = costing.savings_pct(before_usd, after_usd)
    if pct is not None:
        notes.append(f"price improvement: {choice.nodes}x {choice.sku} is {abs(pct):.1f}% "
                     f"{'cheaper' if pct >= 0 else 'more expensive'} than the current "
                     f"{current.nodes}x {current.sku} pool (${before_usd:,.2f} -> ${after_usd:,.2f}/mo)")
    return NodePoolResult(before_sku=current.sku, before_nodes=current.nodes,
                          before_monthly_usd=before_usd, choice=choice,
                          after_monthly_usd=after_usd,
                          monthly_savings_usd=round(before_usd - after_usd, 2),
                          monthly_savings_pct=pct,
                          candidates=candidates, demand_before=before, demand_after=after,
                          node_label=catalog.node_label, migrations=migrations, notes=notes)
