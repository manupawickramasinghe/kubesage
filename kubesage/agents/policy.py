"""What an agent-made change may contain, checked on the objects it renders to: before the Executor commits, and
again before Apply sends anything to the cluster.

edits.py keeps each file parseable. This keeps the content inside the platform's limits: only the kinds the lab
apps use, only the lab namespace, and container resources that are positive, request no more than their limit, and
fit the biggest node the workload can be scheduled on.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from kubesage.apply import kubectl_base

KINDS = {"Deployment", "Service", "ConfigMap", "ScaledObject", "PodDisruptionBudget", "ServiceAccount", "Namespace"}
CLUSTER_SCOPED = {"Namespace"}
_UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60,
          "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "m": 1e-3}
_QTY = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?")


class PolicyError(ValueError):
    pass


def qty(q) -> float:
    """A Kubernetes quantity as a number of cores or bytes. ValueError if it isn't one."""
    m = _QTY.fullmatch(str(q).strip())
    if not m:
        raise ValueError(f"{q!r} is not a Kubernetes quantity")
    return float(m.group(1)) * _UNITS.get(m.group(2) or "", 1)


def biggest_node(nodes: list[dict] | None, pool: str | None) -> dict[str, float] | None:
    """The most cpu and memory any one node of `pool` (or of the cluster) can give a pod."""
    nodes = [n for n in nodes or [] if not pool or n.get("pool") == pool]
    if not nodes:
        return None
    try:
        return {key: max(qty(n["allocatable"][key]) for n in nodes) for key in ("cpu", "memory")}
    except (KeyError, TypeError, ValueError):
        return None


def resources(where: str, res: dict, room: dict[str, float] | None = None) -> list[str]:
    problems = []
    if not isinstance(res, dict):
        return [f"{where}: resources must be a mapping"]
    parsed: dict[str, dict[str, float]] = {}
    for kind in ("requests", "limits"):
        values = res.get(kind) or {}
        if not isinstance(values, dict):
            problems.append(f"{where}: {kind} must be a mapping")
            continue
        for key, value in values.items():
            try:
                n = qty(value)
            except ValueError:
                problems.append(f"{where}: {kind}.{key}={value!r} is not a Kubernetes quantity")
                continue
            if n <= 0:
                problems.append(f"{where}: {kind}.{key}={value} must be more than zero")
            parsed.setdefault(kind, {})[key] = n
            if room and key in room and n > room[key]:
                have = f"{room[key] / 2 ** 30:.1f} GiB" if key == "memory" else f"{room[key]:g} cores"
                problems.append(f"{where}: {kind}.{key}={value} is more than the biggest node can give a pod ({have})")
    for key in ("cpu", "memory"):
        req, lim = parsed.get("requests", {}).get(key), parsed.get("limits", {}).get(key)
        if req is not None and lim is not None and req > lim:
            problems.append(f"{where}: requests.{key}={res['requests'][key]} is above limits.{key}={res['limits'][key]}")
    return problems


def pod_spec(obj: dict) -> dict | None:
    spec = obj.get("spec") or {}
    if obj.get("kind") == "Pod":
        return spec
    return ((spec.get("template") or {}).get("spec")) if isinstance(spec.get("template"), dict) else None


def check(objs: list[dict], namespaces: list[str], nodes: list[dict] | None = None) -> list[str]:
    """Every way `objs` (rendered manifests) step outside the platform's limits; [] when they don't."""
    problems = []
    for obj in objs:
        if not isinstance(obj, dict):
            problems.append("every manifest must be a Kubernetes object")
            continue
        kind, meta = obj.get("kind"), obj.get("metadata") or {}
        name = f"{kind}/{meta.get('name')}"
        if kind not in KINDS:
            problems.append(f"{name}: kind {kind} is not one the lab apps use ({', '.join(sorted(KINDS))})")
            continue
        if kind in CLUSTER_SCOPED:
            if meta.get("name") not in namespaces:
                problems.append(f"{name}: only the {', '.join(namespaces)} namespace may be changed")
        elif meta.get("namespace") not in namespaces:
            problems.append(f"{name}: namespace {meta.get('namespace')!r} is outside {', '.join(namespaces)}")
        pod = pod_spec(obj)
        if pod:
            room = biggest_node(nodes, (pod.get("nodeSelector") or {}).get("kubesage.io/pool"))
            for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
                problems += resources(f"{name} container {c.get('name')}", c.get("resources") or {}, room)
    return problems


def render(target: Path) -> list[dict]:
    """The objects `kubectl apply -k` (a directory) or `-f` (a file) would send. PolicyError if it doesn't build."""
    if target.is_dir():
        try:
            proc = subprocess.run([*kubectl_base(), "kustomize", str(target)], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError(f"kubectl kustomize {target.name}: {exc}") from exc
        if proc.returncode:
            raise PolicyError(f"kubectl kustomize {target.name} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
        text = proc.stdout
    else:
        text = target.read_text()
    try:
        return [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as exc:
        raise PolicyError(f"{target.name}: YAML does not parse: {str(exc).splitlines()[0]}") from exc
