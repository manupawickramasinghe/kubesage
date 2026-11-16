"""Collect + analyze + persist, shared by the CLI and the web UI."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from kubesage.config import ModelsConfig, load_calendar, load_costs, load_models, load_skus
from kubesage.journal import log
from kubesage.llm.client import LLMClient, LLMError
from kubesage.metrics import cluster
from kubesage.metrics.client import PromClient, PromError
from kubesage.metrics.collect import collect
from kubesage.metrics.stats import WorkloadStats
from kubesage.pipeline import AnalysisResult, analyze, write_run


class AnalysisError(RuntimeError):
    pass


def resolve_mode(mode: str, models: ModelsConfig) -> str:
    if mode == "auto":
        return "live" if os.environ.get(models.provider.api_key_env) else "mock"
    return mode


def target_namespaces(namespace: str, client: PromClient, *, days: int, end: float) -> list[str]:
    """`demo`, `demo,staging`, or `*` for every namespace but the shared ones in config/costs.yaml."""
    if namespace.strip() in ("*", "all"):
        shared = set(load_costs().shared_namespaces)
        return [n for n in cluster.namespaces(client, end=end, days=days) if n not in shared]
    return list(dict.fromkeys(n.strip() for n in namespace.split(",") if n.strip()))


def collect_stats(*, vm: str, namespace: str, days: int, tz: str, client: PromClient | None = None,
                  owners=None, end: float | None = None) -> tuple[list[WorkloadStats], int]:
    client, end = client or PromClient(vm), end or time.time()
    stats: list[WorkloadStats] = []
    raw_points = 0
    try:
        names = target_namespaces(namespace, client, days=days, end=end)
        for name in names:
            found, points = collect(client, namespace=name, days=days, end=end, tz=tz, owners=owners)
            if not found and len(names) > 1:
                log("collect", f"no Deployments or StatefulSets with history in namespace {name!r}", stage=1)
            stats += found
            raw_points += points
    except PromError as exc:
        raise AnalysisError(f"metrics query failed: {exc}") from exc
    if not stats:
        where = f"namespace {namespace!r}" if len(names) == 1 else f"any of {len(names)} namespaces"
        raise AnalysisError(f"no workloads found in {where} at {vm}; run `kubesage simulate` first")
    log("collect", f"stage 1: {len(stats)} workloads in {len({s.namespace for s in stats})} namespace(s) from "
                   f"{raw_points:,} raw points over {days} days ($0, no LLM)", stage=1)
    return stats, raw_points


def analyze_now(*, vm: str, namespace: str, days: int, tz: str, mode: str,
                out_dir: str | Path) -> tuple[AnalysisResult, Path, ModelsConfig]:
    models = load_models()
    prom = PromClient(vm)
    end = time.time()
    catalog, notes, others = load_skus(), [], []
    try:
        # The rest of the cluster: the node pool has to hold it too, and the plan starts from the real nodes.
        inv = cluster.inventory(prom, catalog=catalog, end=end, days=days)
    except PromError as exc:
        inv = cluster.Inventory(notes=[f"Could not read the cluster inventory ({exc}): the node pool is sized for "
                                       "the analysed namespaces alone."])
    stats, raw_points = collect_stats(vm=vm, namespace=namespace, days=days, tz=tz, client=prom, owners=inv.owners, end=end)
    notes += inv.notes
    if inv.pool is not None:
        catalog = catalog.model_copy(update={"current_pool": inv.pool})
    if inv.at is not None:
        others = cluster.background(inv, stats)
        log("collect", f"cluster inventory: {len(inv.pods)} pods, {len(others)} outside this analysis"
                       + (f", nodes {inv.pool.nodes}x {inv.pool.sku}" if inv.pool else ""), stage=1)
    try:
        llm = LLMClient(models, resolve_mode(mode, models))
        result = analyze(stats, llm=llm, catalog=catalog, namespace=",".join(sorted({s.namespace for s in stats})),
                         days=days, tz=tz, raw_points=raw_points, calendar=load_calendar(), background=others, notes=notes)
    except LLMError as exc:
        raise AnalysisError(f"LLM call failed: {exc}") from exc
    run_dir = write_run(result, Path(out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S-%f"), trace=prom.calls + llm.trace)
    log("collect", f"run {run_dir.name} written: {sum(len(w.manifests) for w in result.workloads)} manifests, "
                   f"LLM ${result.llm_cost_usd:.4f} over {len(result.ledger)} calls", stage=3)
    return result, run_dir, models
