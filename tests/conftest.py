"""Shared fixtures: the demo scenarios reduced to Stage 1 stats, and a mock-mode analysis of them."""
import numpy as np
import pytest

from kubesage.config import load_models, load_skus
from kubesage.llm.client import LLMClient
from kubesage.metrics.stats import summarize
from kubesage.pipeline import analyze
from kubesage.simulate.backfill import time_axis
from kubesage.simulate.scenarios import DEMO_WORKLOADS

TZ = "Asia/Colombo"
END = 1_790_000_000 // 300 * 300


@pytest.fixture(scope="session")
def stats():
    t = time_axis(END - 30 * 86400, END, 300, TZ)
    rng = np.random.default_rng(7)
    out = []
    for w in DEMO_WORKLOADS:
        pods = [w.pattern(t, w, np.random.default_rng(rng.integers(2**32))) for _ in range(w.replicas)]
        cpu = np.stack([p[0] for p in pods])
        mem = np.stack([p[1] for p in pods])
        restarts = np.stack([p[2] for p in pods]).max(axis=0)
        out.append(summarize(namespace="demo", name=w.name, ts=t.ts.astype(float), cpu_pod=cpu.max(axis=0),
                             cpu_total=cpu.sum(axis=0), mem_pod=mem.max(axis=0), restarts=restarts,
                             replicas=w.replicas, cpu_request=w.cpu_request, cpu_limit=w.cpu_limit,
                             mem_request=w.mem_request, mem_limit=w.mem_limit, tz=TZ))
    return {s.name: s for s in out}


@pytest.fixture(scope="session")
def result(stats):
    llm = LLMClient(load_models(), "mock")
    return analyze(list(stats.values()), llm=llm, catalog=load_skus(), namespace="demo", days=30, tz=TZ,
                   raw_points=10_000)
