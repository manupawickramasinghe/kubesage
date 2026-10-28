"""Live test cases: each one deploys a real workload into the `lab` namespace that causes one kind of problem.

The workloads live in gitops/apps/ (the desired state the Executor agent edits). The security test case is the
exception: it is created outside GitOps on purpose, from kubesage/lab/debug-tools.yaml.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

import yaml

from kubesage.apply import kubectl_base

NAMESPACE = "lab"
GITOPS = Path(__file__).resolve().parents[2] / "gitops"
APP_IMAGE = "python:3.13.12-alpine3.22"


@dataclass(frozen=True)
class TestCase:
    name: str
    title: str
    category: str
    app: str
    summary: str
    expect: str
    gitops: bool = True    # False: created out of band from kubesage/lab/<app>.yaml
    load_rps: float = 0    # an in-cluster load generator against the app's Service, if > 0

    def as_dict(self) -> dict:
        return asdict(self)


CATALOG = [
    TestCase("memory", "High memory", "memory", "ledger-cache",
             "ledger-cache loads 700 MiB of ledger into RAM under a 512 MiB limit, so it is OOM-killed and restarts.",
             "Raise the memory limit; a memory-optimised SKU (E-series) for the pool."),
    TestCase("cpu-throttle", "High CPU: throttled", "cpu", "render-farm",
             "render-farm runs 4 CPU-bound worker processes under a 1-core limit, so CFS throttles it most of the time.",
             "Give it the cores it needs; a compute-optimised SKU (F-series) if the pool is short of CPU."),
    TestCase("cpu-code", "High CPU: single-threaded code", "cpu", "risk-scorer",
             "risk-scorer requests 3 cores but its Python thread pool is bound by the GIL, so it can only ever use one.",
             "A bigger SKU won't help: change the code to a process pool, then it uses all 3 cores."),
    TestCase("disk-io", "Disk I/O", "disk_io", "log-shipper",
             "log-shipper writes 20 MB/s to local disk, fsyncing every 256 KiB block.",
             "Batch the fsyncs or move the spool to faster storage (L-series local NVMe)."),
    TestCase("disk-full", "Disk storage", "disk_space", "media-cache",
             "media-cache fills its 1 GiB scratch volume to 950 MiB and never evicts anything.",
             "Add eviction to the cache, or give it a bigger volume before kubelet evicts the pod."),
    TestCase("latency", "Latency", "latency", "api-gateway",
             "A load generator sends 25 req/s of 30 ms CPU work to api-gateway, which has a 0.3-core limit.",
             "Raise the CPU limit and scale out (KEDA) so p95 latency drops back under 500 ms.", load_rps=25),
    TestCase("pod-count", "Pod count: Pending", "pod_count", "batch-fanout",
             "batch-fanout asks for 12 replicas at 1 core each on an 8-core node, so several stay Pending.",
             "The pods use almost no CPU: cut the requests rather than buying nodes."),
    TestCase("pod-density", "Pod density", "pod_density", "sidecar-swarm",
             "sidecar-swarm packs 80 tiny pods onto the one lab node (max 110 pods).",
             "Spread or consolidate the pods, or raise max_pods on the pool."),
    TestCase("security", "Security: hand-made pod", "security", "debug-tools",
             "A bare Pod started outside GitOps: busybox:latest, running as root, no resource limits.",
             "Flag the drift and the misconfiguration; enforce Pod Security on the namespace.", gitops=False),
]
BY_NAME = {tc.name: tc for tc in CATALOG}


def _kubectl(*args: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([*kubectl_base(), "--request-timeout=20s", *args], input=stdin, capture_output=True,
                          text=True, timeout=timeout)


def _step(cmd: str, proc: subprocess.CompletedProcess) -> dict:
    return {"cmd": cmd, "ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()[-2000:]}


def manifest_args(tc: TestCase, gitops: Path = GITOPS) -> list[str]:
    if tc.gitops:
        return ["-k", str(gitops / "apps" / tc.app)]
    return ["-f", str(resources.files("kubesage.lab").joinpath(f"{tc.app}.yaml"))]


def load_job(tc: TestCase) -> dict:
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"{tc.app}-load", "namespace": NAMESPACE,
                     "labels": {"app": f"{tc.app}-load", "kubesage.io/testcase": tc.name}},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 300, "template": {
            "metadata": {"labels": {"app": f"{tc.app}-load", "kubesage.io/testcase": tc.name},
                         "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": "9102"}},
            "spec": {"restartPolicy": "Never", "nodeSelector": {"kubesage.io/pool": "lab"},
                     "terminationGracePeriodSeconds": 1, "containers": [{
                         "name": "loadgen", "image": APP_IMAGE, "command": ["python", "-u", "/load/loadgen.py"],
                         "env": [{"name": "TARGET", "value": f"http://{tc.app}:8080/api"},
                                 {"name": "TARGET_APP", "value": tc.app},
                                 {"name": "RPS", "value": str(tc.load_rps)}, {"name": "SECONDS", "value": "900"}],
                         "volumeMounts": [{"name": "load", "mountPath": "/load"}],
                         "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}}],
                     "volumes": [{"name": "load", "configMap": {"name": "lab-loadgen"}}]}}},
    }


def start(tc: TestCase, gitops: Path = GITOPS) -> list[dict]:
    steps = [_step(f"kubectl apply -f gitops/apps/namespace.yaml",
                   _kubectl("apply", "-f", str(gitops / "apps" / "namespace.yaml")))]
    where = f"gitops/apps/{tc.app}" if tc.gitops else f"kubesage/lab/{tc.app}.yaml"
    steps.append(_step(f"kubectl apply {'-k' if tc.gitops else '-f'} {where}", _kubectl("apply", *manifest_args(tc, gitops))))
    if tc.load_rps and steps[-1]["ok"]:
        src = resources.files("kubesage.lab").joinpath("loadgen.py").read_text()
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "lab-loadgen", "namespace": NAMESPACE},
              "data": {"loadgen.py": src}}
        _kubectl("-n", NAMESPACE, "delete", "job", f"{tc.app}-load", "--ignore-not-found", "--wait=true")
        steps.append(_step(f"kubectl apply job/{tc.app}-load ({tc.load_rps:g} req/s)",
                           _kubectl("apply", "-f", "-", stdin=yaml.safe_dump_all([cm, load_job(tc)]))))
    return steps


def stop(tc: TestCase, gitops: Path = GITOPS) -> list[dict]:
    steps = []
    if tc.load_rps:
        steps.append(_step(f"kubectl delete job/{tc.app}-load",
                           _kubectl("-n", NAMESPACE, "delete", "job", f"{tc.app}-load", "--ignore-not-found", "--wait=false")))
    where = f"gitops/apps/{tc.app}" if tc.gitops else f"kubesage/lab/{tc.app}.yaml"
    steps.append(_step(f"kubectl delete {'-k' if tc.gitops else '-f'} {where}",
                       _kubectl("delete", *manifest_args(tc, gitops), "--ignore-not-found", "--wait=false")))
    return steps


def status(pods: dict | None) -> dict[str, dict]:
    """Per test case: its pods' phases in the lab namespace, from `kubectl get pods -n lab -o json`."""
    by_app: dict[str, list[dict]] = {}
    for pod in (pods or {}).get("items", []):
        by_app.setdefault(pod["metadata"].get("labels", {}).get("app", ""), []).append(pod)
    out = {}
    for tc in CATALOG:
        items = by_app.get(tc.app, [])
        if not items:
            continue
        phases: dict[str, int] = {}
        restarts = 0
        for pod in items:
            phase = "Terminating" if pod["metadata"].get("deletionTimestamp") else pod.get("status", {}).get("phase", "Unknown")
            phases[phase] = phases.get(phase, 0) + 1
            restarts += sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
        started = min(p["metadata"]["creationTimestamp"] for p in items)
        out[tc.name] = {"pods": len(items), "phases": phases, "restarts": restarts, "since": started,
                        "stopping": all(p["metadata"].get("deletionTimestamp") for p in items)}
    return out


def lab_pods() -> dict | None:
    try:
        proc = _kubectl("-n", NAMESPACE, "get", "pods", "-o", "json", timeout=15)
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None

