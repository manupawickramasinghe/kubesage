"""Apply an analysis run's manifests to a cluster with kubectl.

Order: resource patches (each after removing an autoscaler it replaces), then node-pool migrations, then
ScaledObjects. A migration is skipped unless some node carries the target SKU label, so pods never go Pending on a
cluster that lacks the cheaper nodes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_BIN = Path(__file__).resolve().parent.parent / ".bin"
MANAGED_LABEL = "app.kubernetes.io/managed-by=kubesage"
NO_KIND = "doesn't have a resource type"


@dataclass
class Step:
    file: Path
    cmd: list[str]
    returncode: int | None = None
    output: str = ""
    skipped: str | None = None


def latest_run(out_dir: str | Path = "out") -> Path:
    runs = sorted(p for p in Path(out_dir).glob("*/")
                  if not p.name.startswith(".") and (p / "manifests").is_dir() and (p / "report.json").is_file())
    if not runs:
        raise FileNotFoundError(f"no analysis runs under {out_dir}/; run `kubesage analyze` first")
    return runs[-1]


def kubectl_base() -> list[str]:
    """Prefer the repo's pinned kubectl and the local demo cluster's kubeconfig, unless overridden."""
    binary = os.environ.get("KUBECTL") or (str(REPO_BIN / "kubectl") if (REPO_BIN / "kubectl").exists() else None) \
        or shutil.which("kubectl") or "kubectl"
    cmd = [binary]
    if "KUBECONFIG" not in os.environ and (REPO_BIN / "kubeconfig").exists():
        cmd += ["--kubeconfig", str(REPO_BIN / "kubeconfig")]
    return cmd


def node_skus(label: str, kubectl: list[str] | None = None) -> set[str]:
    """Values of `label` across the cluster's nodes (empty if the cluster can't be reached)."""
    try:
        proc = subprocess.run([*(kubectl or kubectl_base()), "get", "nodes", "-o", "json"],
                              capture_output=True, text=True, timeout=12)
        if proc.returncode != 0:
            return set()
        return {n["metadata"].get("labels", {}).get(label) for n in json.loads(proc.stdout)["items"]} - {None}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return set()


def ready_node_counts(label: str, kubectl: list[str] | None = None) -> dict[str, int]:
    """Schedulable, Ready nodes per SKU; unreachable clusters provide no migration capacity."""
    try:
        proc = subprocess.run([*(kubectl or kubectl_base()), "get", "nodes", "-o", "json"],
                              capture_output=True, text=True, timeout=12)
        if proc.returncode != 0:
            return {}
        nodes = json.loads(proc.stdout)["items"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return {}
    return dict(Counter(sku for n in nodes
                        if not n.get("spec", {}).get("unschedulable")
                        and any(c.get("type") == "Ready" and c.get("status") == "True"
                                for c in n.get("status", {}).get("conditions", []))
                        if (sku := n.get("metadata", {}).get("labels", {}).get(label))))


def plan(run_dir: Path, *, dry_run: bool = False, kubectl: list[str] | None = None,
         available_skus: set[str] | None = None,
         available_node_counts: dict[str, int] | None = None) -> list[Step]:
    kubectl = kubectl or kubectl_base()
    extra = ["--dry-run=server"] if dry_run else []
    manifests = Path(run_dir) / "manifests"
    report_file = Path(run_dir) / "report.json"
    choice = ((json.loads(report_file.read_text()).get("node_pool") or {}).get("choice") or {}) \
        if report_file.exists() else {}

    def patch(f: Path) -> Step:
        meta = yaml.safe_load(f.read_text())["metadata"]
        return Step(f, [*kubectl, "-n", meta["namespace"], "patch", "deployment", meta["name"],
                        "--type", "strategic", "--patch-file", str(f), *extra])

    def unscale(f: Path) -> Step | None:
        # A fixed replica count replaces autoscaling: remove the ScaledObject an earlier run applied, or the HPA
        # KEDA made for it keeps resizing the Deployment the patch just pinned.
        doc = yaml.safe_load(f.read_text())
        meta = doc["metadata"]
        if (doc.get("spec") or {}).get("replicas") is None or (manifests / f"{meta['name']}.keda.yaml").exists():
            return None
        return Step(f, [*kubectl, "-n", meta["namespace"], "delete", "scaledobjects.keda.sh", "-l", MANAGED_LABEL,
                        "--field-selector", f"metadata.name={meta['name']}", "--ignore-not-found", *extra])

    steps = [step for f in sorted(manifests.glob("*.patch.yaml")) for step in (unscale(f), patch(f)) if step]
    for f in sorted(manifests.glob("*.migrate.yaml")):
        step = patch(f)
        selector = yaml.safe_load(f.read_text())["spec"]["template"]["spec"]["nodeSelector"]
        (label, sku), = selector.items()
        if available_node_counts is not None:
            required = max(1, int(choice.get("nodes", 1))) if choice.get("sku") == sku else 1
            available = available_node_counts.get(sku, 0)
            if available < required:
                step.skipped = (f"only {available}/{required} Ready, schedulable nodes labelled {label}={sku}; "
                                "provision the planned capacity before migrating")
        elif available_skus is not None and sku not in available_skus:
            step.skipped = f"no node labelled {label}={sku} (start the cluster with TWO_NODE=1)"
        steps.append(step)
    steps += [Step(f, [*kubectl, "apply", "-f", str(f), *extra]) for f in sorted(manifests.glob("*.keda.yaml"))]
    return steps


def run(steps: list[Step]) -> list[Step]:
    failed = None
    for step in steps:
        if step.skipped:
            continue
        if failed:
            step.skipped = f"not run because {failed} failed; review the error before retrying"
            continue
        try:
            proc = subprocess.run(step.cmd, capture_output=True, text=True, timeout=60)
            step.returncode, step.output = proc.returncode, (proc.stdout + proc.stderr).strip()
        except subprocess.TimeoutExpired:
            step.returncode, step.output = 124, "kubectl timed out after 60 seconds; check the cluster before retrying"
        except OSError as exc:
            step.returncode, step.output = 127, f"could not start kubectl: {exc}"
        if step.returncode and "scaledobjects.keda.sh" in step.cmd and NO_KIND in step.output:
            step.returncode, step.output = 0, "KEDA isn't installed here, so there is no ScaledObject to remove"
        if step.returncode != 0:
            failed = step.file.name
    return steps
