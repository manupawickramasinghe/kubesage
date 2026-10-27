"""Collect + analyze + persist, shared by the CLI and the web UI."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from kubesage.config import ModelsConfig, load_models, load_skus
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


def collect_stats(*, vm: str, namespace: str, days: int, tz: str) -> tuple[list[WorkloadStats], int]:
    try:
        stats, raw_points = collect(PromClient(vm), namespace=namespace, days=days, end=time.time(), tz=tz)
    except PromError as exc:
        raise AnalysisError(f"metrics query failed: {exc}") from exc
    if not stats:
        raise AnalysisError(f"no workloads found in namespace {namespace!r} at {vm}; run `kubesage simulate` first")
    return stats, raw_points


def analyze_now(*, vm: str, namespace: str, days: int, tz: str, mode: str,
                out_dir: str | Path) -> tuple[AnalysisResult, Path, ModelsConfig]:
    models = load_models()
    stats, raw_points = collect_stats(vm=vm, namespace=namespace, days=days, tz=tz)
    try:
        result = analyze(stats, llm=LLMClient(models, resolve_mode(mode, models)), catalog=load_skus(),
                         namespace=namespace, days=days, tz=tz, raw_points=raw_points)
    except LLMError as exc:
        raise AnalysisError(f"LLM call failed: {exc}") from exc
    run_dir = write_run(result, Path(out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    return result, run_dir, models
