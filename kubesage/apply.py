"""Apply an analysis run's manifests to a cluster with kubectl: Deployment patches first, then ScaledObjects."""
from __future__ import annotations

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


def plan(run_dir: Path, *, dry_run: bool = False, kubectl: list[str] | None = None) -> list[Step]:
    kubectl = kubectl or kubectl_base()
    extra = ["--dry-run=server"] if dry_run else []
    manifests = Path(run_dir) / "manifests"
    steps = []
    for f in sorted(manifests.glob("*.patch.yaml")):
        meta = yaml.safe_load(f.read_text())["metadata"]
        steps.append(Step(f, [*kubectl, "-n", meta["namespace"], "patch", "deployment", meta["name"],
                              "--type", "strategic", "--patch-file", str(f), *extra]))
    for f in sorted(manifests.glob("*.keda.yaml")):
        steps.append(Step(f, [*kubectl, "apply", "-f", str(f), *extra]))
    return steps


def run(steps: list[Step]) -> list[Step]:
    for step in steps:
        proc = subprocess.run(step.cmd, capture_output=True, text=True)
        step.returncode, step.output = proc.returncode, (proc.stdout + proc.stderr).strip()
    return steps
