"""Collect + analyze + persist, shared by the CLI and the web UI."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from kubesage.config import ModelsConfig, load_models, load_skus
from kubesage.journal import log
from kubesage.llm.client import LLMClient, LLMError
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


def collect_stats(*, vm: str, namespace: str, days: int, tz: str,
                  client: PromClient | None = None) -> tuple[list[WorkloadStats], int]:
    try:
        stats, raw_points = collect(client or PromClient(vm), namespace=namespace, days=days, end=time.time(), tz=tz)
    except PromError as exc:
        raise AnalysisError(f"metrics query failed: {exc}") from exc
    if not stats:
        raise AnalysisError(f"no workloads found in namespace {namespace!r} at {vm}; run `kubesage simulate` first")
    log("collect", f"stage 1: {len(stats)} workloads from {raw_points:,} raw points over {days} days ($0, no LLM)", stage=1)
    return stats, raw_points


def analyze_now(*, vm: str, namespace: str, days: int, tz: str, mode: str,
                out_dir: str | Path) -> tuple[AnalysisResult, Path, ModelsConfig]:
    models = load_models()
    prom = PromClient(vm)
    stats, raw_points = collect_stats(vm=vm, namespace=namespace, days=days, tz=tz, client=prom)
    try:
        llm = LLMClient(models, resolve_mode(mode, models))
        result = analyze(stats, llm=llm, catalog=load_skus(), namespace=namespace, days=days, tz=tz, raw_points=raw_points)
    except LLMError as exc:
        raise AnalysisError(f"LLM call failed: {exc}") from exc
    run_dir = write_run(result, Path(out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S-%f"), trace=prom.calls + llm.trace)
    log("collect", f"run {run_dir.name} written: {sum(len(w.manifests) for w in result.workloads)} manifests, "
                   f"LLM ${result.llm_cost_usd:.4f} over {len(result.ledger)} calls", stage=3)
    return result, run_dir, models
