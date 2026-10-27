"""Apply an analysis run's manifests to a cluster with kubectl.

Order: resource patches, then node-pool migrations, then ScaledObjects. A migration is skipped unless some
node carries the target SKU label, so pods never go Pending on a cluster that lacks the cheaper nodes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_BIN = Path(__file__).resolve().parent.parent / ".bin"


@dataclass
class Step:
    file: Path
    cmd: list[str]
    returncode: int | None = None
    output: str = ""
    skipped: str | None = None


def latest_run(out_dir: str | Path = "out") -> Path:
    runs = sorted(p for p in Path(out_dir).glob("*/") if (p / "manifests").is_dir())
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
    proc = subprocess.run([*(kubectl or kubectl_base()), "get", "nodes", "-o", "json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return set()
    return {n["metadata"].get("labels", {}).get(label) for n in json.loads(proc.stdout)["items"]} - {None}


def plan(run_dir: Path, *, dry_run: bool = False, kubectl: list[str] | None = None,
         available_skus: set[str] | None = None) -> list[Step]:
    kubectl = kubectl or kubectl_base()
    extra = ["--dry-run=server"] if dry_run else []
    manifests = Path(run_dir) / "manifests"

    def patch(f: Path) -> Step:
        meta = yaml.safe_load(f.read_text())["metadata"]
        return Step(f, [*kubectl, "-n", meta["namespace"], "patch", "deployment", meta["name"],
                        "--type", "strategic", "--patch-file", str(f), *extra])

    steps = [patch(f) for f in sorted(manifests.glob("*.patch.yaml"))]
    for f in sorted(manifests.glob("*.migrate.yaml")):
        step = patch(f)
        selector = yaml.safe_load(f.read_text())["spec"]["template"]["spec"]["nodeSelector"]
        (label, sku), = selector.items()
        if available_skus is not None and sku not in available_skus:
            step.skipped = f"no node labelled {label}={sku} (start the cluster with TWO_NODE=1)"
        steps.append(step)
    steps += [Step(f, [*kubectl, "apply", "-f", str(f), *extra]) for f in sorted(manifests.glob("*.keda.yaml"))]
    return steps


def run(steps: list[Step]) -> list[Step]:
    for step in steps:
        if step.skipped:
            continue
        proc = subprocess.run(step.cmd, capture_output=True, text=True)
        step.returncode, step.output = proc.returncode, (proc.stdout + proc.stderr).strip()
    return steps
