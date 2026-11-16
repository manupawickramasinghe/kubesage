"""Render validated recommendations into Kubernetes manifests (strategic-merge patches + KEDA ScaledObjects)."""
from __future__ import annotations

import yaml

from kubesage.llm.schemas import Recommendation

MANAGED_BY = {"app.kubernetes.io/managed-by": "kubesage"}
ANNOTATION = "kubesage.io/dated-events"  # what the dated cron triggers are for, in words


def cpu_qty(millicores: int) -> str:
    return str(millicores // 1000) if millicores % 1000 == 0 else f"{millicores}m"


def mem_qty(mib: int) -> str:
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False)


def render(rec: Recommendation, *, namespace: str, name: str, container: str, tz: str,
           dated: list | None = None, current_replicas: int = 1, kind: str = "Deployment",
           stem: str | None = None) -> dict[str, str]:
    """Return {filename: yaml}. Patches apply with `kubectl patch <kind> --type strategic --patch-file`.

    `kind` is the controller (Deployment or StatefulSet) and `container` the container the recommendation is
    for. `stem` names the files; it defaults to the workload's name, and a run over several namespaces passes
    `<namespace>.<name>` so two namespaces' workloads of the same name don't overwrite each other.

    `dated` are the calendar's dated events as KEDA cron triggers (recommend/timed.py). They join the
    ScaledObject the plan asks for; a workload whose plan is a fixed replica count, or no scaling change at
    all, gets a ScaledObject that holds that count and only moves for the events."""
    files: dict[str, str] = {}
    stem = stem or name
    pod_spec: dict = {}
    if rec.resources is not None:
        r = rec.resources
        limits = {"cpu": cpu_qty(r.cpu_limit_m) if r.cpu_limit_m else None, "memory": mem_qty(r.mem_limit_mi)}
        pod_spec["containers"] = [{"name": container, "resources": {
            "requests": {"cpu": cpu_qty(r.cpu_request_m), "memory": mem_qty(r.mem_request_mi)}, "limits": limits}}]

    plan = rec.scaling
    floor = plan.min_replicas if plan is not None else current_replicas
    # An event that needs no more replicas than the workload always has changes nothing: leave it out.
    dated = [d for d in (dated or []) if d.replicas > floor]
    patch: dict = {}
    if plan is not None and not plan.cron_windows and plan.cpu_utilization_target is None and not dated:
        patch["replicas"] = plan.min_replicas  # fixed replica count: no autoscaler needed
    elif plan is not None or dated:
        ceiling = max([plan.max_replicas if plan is not None else current_replicas, floor, 1, *(d.replicas for d in dated)])
        cron = lambda start, end, replicas: {"type": "cron", "metadata": {
            "timezone": tz, "start": start, "end": end, "desiredReplicas": str(replicas)}}
        triggers = [cron(w.start, w.end, w.desired_replicas) for w in (plan.cron_windows if plan is not None else [])]
        triggers += [cron(d.start, d.end, d.replicas) for d in dated]
        if plan is not None and plan.cpu_utilization_target is not None:
            triggers.append({"type": "cpu", "metricType": "Utilization",
                             "metadata": {"value": str(plan.cpu_utilization_target)}})
        metadata: dict = {"name": name, "namespace": namespace, "labels": MANAGED_BY}
        if dated:
            metadata["annotations"] = {ANNOTATION: "; ".join(dict.fromkeys(d.label for d in dated))}
        target = {"name": name} if kind == "Deployment" else {"kind": kind, "name": name}  # KEDA defaults to Deployment
        files[f"{stem}.keda.yaml"] = _dump({
            "apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject", "metadata": metadata,
            "spec": {"scaleTargetRef": target, "minReplicaCount": floor,
                     "maxReplicaCount": ceiling, "triggers": triggers}})
    if pod_spec:
        patch["template"] = {"spec": pod_spec}
    if patch:
        files[f"{stem}.patch.yaml"] = _dump(workload_patch(kind, namespace, name, patch))
    return files


def migration(namespace: str, name: str, label: str, sku: str, kind: str = "Deployment") -> str:
    """Patch that moves a workload's pods onto nodes of another SKU."""
    return _dump(workload_patch(kind, namespace, name, {"template": {"spec": {"nodeSelector": {label: sku}}}}))


def workload_patch(kind: str, namespace: str, name: str, spec: dict) -> dict:
    """A strategic-merge patch with a full header, so it works with both `kubectl patch` and kustomize."""
    return {"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": name, "namespace": namespace}, "spec": spec}
