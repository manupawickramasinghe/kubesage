"""The demo cluster's "before" manifests must match the scenarios the history is simulated from."""
from pathlib import Path

import yaml

from kubesage.simulate.scenarios import DEMO_WORKLOADS

MANIFEST = Path(__file__).resolve().parent.parent / "deploy/k8s/demo/workloads.yaml"


def _cores(q: str) -> float:
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def _bytes(q: str) -> int:
    units = {"Mi": 1024**2, "Gi": 1024**3}
    return int(float(q[:-2]) * units[q[-2:]])


def test_demo_manifests_match_scenarios():
    deployments = {d["metadata"]["name"]: d for d in yaml.safe_load_all(MANIFEST.read_text()) if d["kind"] == "Deployment"}
    assert set(deployments) == {w.name for w in DEMO_WORKLOADS}
    for w in DEMO_WORKLOADS:
        d = deployments[w.name]
        assert d["spec"]["replicas"] == w.replicas
        (c,) = d["spec"]["template"]["spec"]["containers"]
        assert c["name"] == w.name
        res = c["resources"]
        assert _cores(res["requests"]["cpu"]) == w.cpu_request
        assert _cores(res["limits"]["cpu"]) == w.cpu_limit
        assert _bytes(res["requests"]["memory"]) == w.mem_request
        assert _bytes(res["limits"]["memory"]) == w.mem_limit


def test_demo_workloads_start_on_the_current_pool():
    from kubesage.config import load_skus

    catalog = load_skus()
    for d in yaml.safe_load_all(MANIFEST.read_text()):
        if d["kind"] == "Deployment":
            assert d["spec"]["template"]["spec"]["nodeSelector"] == {catalog.node_label: catalog.current_pool.sku}
