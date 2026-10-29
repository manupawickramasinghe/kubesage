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


def rooms(nodes: list[dict] | None, pool: str | None) -> list[dict[str, float]]:
    """What each node of `pool` (or of the cluster) can give a pod: cpu cores and memory bytes; [] when unknown."""
    out = []
    for n in nodes or []:
        if pool and n.get("pool") != pool:
            continue
        try:
            out.append({key: qty(n["allocatable"][key]) for key in ("cpu", "memory")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def pod_demand(pod: dict, kind: str) -> dict[str, float]:
    """A pod's `kind` (requests or limits) as the scheduler adds it up: its containers' sum, or its largest init
    container if that is more. Unparseable values count as zero here; resources() reports them."""
    def total(cs):
        out = {"cpu": 0.0, "memory": 0.0}
        for c in cs:
            for key in out:
                try:
                    out[key] += qty((((c.get("resources") or {}).get(kind) or {}).get(key)) or 0)
                except (ValueError, AttributeError):
                    pass
        return out
    main = total(pod.get("containers") or [])
    for c in pod.get("initContainers") or []:
        one = total([c])
        main = {key: max(main[key], one[key]) for key in main}
    return main


def fits(need: dict[str, float], room: list[dict[str, float]]) -> bool:
    """True when one node has room for all of it: cpu and memory on the same node, not maxima from two."""
    return any(all(need[k] <= r[k] for k in need) for r in room)


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
            pool = (pod.get("nodeSelector") or {}).get("kubesage.io/pool")
            for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
                problems += resources(f"{name} container {c.get('name')}", c.get("resources") or {})
            room = rooms(nodes, pool)
            if not room:  # fail closed: a pod that can't be checked against a real node isn't accepted
                problems.append(f"{name}: no node inventory for {f'pool {pool}' if pool else 'the cluster'}, so it can't "
                                f"be checked that the pod fits a node")
                continue
            for kind in ("requests", "limits"):
                need = pod_demand(pod, kind)
                if not fits(need, room):
                    problems.append(f"{name}: the pod's {kind} add up to {need['cpu']:g} cores and "
                                    f"{need['memory'] / 2 ** 30:.1f} GiB, more than any one "
                                    f"{f'{pool} ' if pool else ''}node can give it")
    return problems


# ---- what an approved action may change ----

# Per action type: the kinds of object it may change and, as a regex over the flattened path, where in them.
# Lists of named items are keyed by name: spec.template.spec.containers[ledger-cache].resources.limits.memory.
_C = r"spec\.template\.spec\.(?:initContainers|containers)\[[^\]]+\]\."
CHANGES = {
    "set_resources": [("Deployment", _C + r"resources\.")],
    "storage": [("Deployment", _C + r"(?:resources\.|volumeMounts\[|env\[)"),
                ("Deployment", r"spec\.template\.spec\.volumes\[[^\]]+\]\.(?:name$|emptyDir)")],
    "code_change": [("ConfigMap", r"data\."), ("Deployment", _C + r"(?:env\[|command|args)"),
                    ("Deployment", r"spec\.template\.metadata\.annotations\.")],
    "security_hardening": [("Deployment", r"spec\.template\.spec\.(?:securityContext|automountServiceAccountToken)"),
                           ("Deployment", _C + r"securityContext\."),
                           ("Namespace", r"metadata\.labels\.pod-security\.kubernetes\.io/")],
    "autoscale": [("ScaledObject", r""), ("Deployment", r"spec\.replicas$")],
    "spread_pods": [("Deployment", r"spec\.replicas$|spec\.template\.spec\.(?:topologySpreadConstraints|affinity)"),
                    ("PodDisruptionBudget", r"")],
}
# Values no action may introduce, whatever its type allows.
NEVER = [(r"\.privileged$", lambda v: v is True), (r"\.allowPrivilegeEscalation$", lambda v: v is True),
         (r"\.capabilities\.add", lambda v: v not in (None, [], {})), (r"\.runAsUser$", lambda v: v == 0),
         (r"\.runAsNonRoot$", lambda v: v is False), (r"\.hostPath\.", lambda v: v is not None),
         (r"spec\.template\.spec\.host(?:Network|PID|IPC)$", lambda v: v is True)]
_GENERATED = re.compile(r"^(.+)-[a-z0-9]{10}$")  # a kustomize configMapGenerator name: base-<content hash>


def _flatten(value, path: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _flatten(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(value, list) and value:
        named = all(isinstance(x, dict) and isinstance(x.get("name"), str) for x in value)
        for i, x in enumerate(value):
            _flatten(x, f"{path}[{x['name'] if named else i}]", out)
    else:
        out[path] = value
    return out


def _objects(objs: list[dict]) -> dict[tuple[str, str], dict]:
    """Objects by (kind, name), with generated ConfigMap names reduced to their base wherever they appear, so new
    source content (a new hash) reads as a change of data, not as a new object plus a new reference."""
    generated = {o["metadata"]["name"]: m.group(1) for o in objs if isinstance(o, dict) and o.get("kind") == "ConfigMap"
                 for m in [_GENERATED.match(str((o.get("metadata") or {}).get("name", "")))] if m}

    def plain(v):
        if isinstance(v, dict):
            return {k: plain(x) for k, x in v.items()}
        if isinstance(v, list):
            return [plain(x) for x in v]
        return generated.get(v, v) if isinstance(v, str) else v
    return {(o.get("kind"), plain((o.get("metadata") or {}).get("name"))): plain(o) for o in objs if isinstance(o, dict)}


def _owner(action, kind: str, name: str, obj: dict) -> bool:
    """Is this object the action's own: its workload, that workload's source, autoscaler or PDB, or its namespace."""
    target = action.target.split("/")
    if target[0] == "namespace":
        return kind == "Namespace" and name == target[-1]
    app = target[-1]
    if kind == "ScaledObject":
        return name == app or ((obj.get("spec") or {}).get("scaleTargetRef") or {}).get("name") == app
    return name == app or (kind == "ConfigMap" and name == f"{app}-src") or (kind == "PodDisruptionBudget" and name.startswith(app))


def unapproved(before: list[dict], after: list[dict], actions: list) -> list[str]:
    """Every change between the rendered objects before and after the edits that no approved action covers: the
    whole diff, not just the values an action named. [] when each change belongs to an approved action."""
    old, new = _objects(before), _objects(after)
    problems = []
    for key in sorted(set(old) | set(new), key=str):
        kind, name = key
        a, b = _flatten(old.get(key, {})), _flatten(new.get(key, {}))
        changed = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
        if not changed:
            continue
        if key not in new:
            problems.append(f"{kind}/{name} is removed, and no action may remove objects")
            continue
        for path in changed:
            value = b.get(path)
            bad = next((rx for rx, test in NEVER if re.search(rx, path) and test(value)), None)
            if bad:
                problems.append(f"{kind}/{name}: {path}={value!r} is never allowed")
                continue
            if not any(_owner(act, kind, name, new[key]) and k == kind and re.match(rx, path)
                       for act in actions for k, rx in CHANGES.get(act.type, [])):
                what = "added" if key not in old else f"{path} changed"
                problems.append(f"{kind}/{name}: {what}, which no approved action ({', '.join(f'{x.id} {x.type}' for x in actions)}) covers")
                if key not in old:
                    break  # one line per object nobody asked for
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
