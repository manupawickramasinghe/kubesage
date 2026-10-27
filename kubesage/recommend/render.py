"""Render validated recommendations into Kubernetes manifests (strategic-merge patches + KEDA ScaledObjects)."""
from __future__ import annotations

import yaml

from kubesage.llm.schemas import Recommendation

MANAGED_BY = {"app.kubernetes.io/managed-by": "kubesage"}


def cpu_qty(millicores: int) -> str:
    return str(millicores // 1000) if millicores % 1000 == 0 else f"{millicores}m"


def mem_qty(mib: int) -> str:
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False)


def render(rec: Recommendation, *, namespace: str, name: str, container: str, tz: str) -> dict[str, str]:
    """Return {filename: yaml}. Patches apply with `kubectl patch deployment --type strategic --patch-file`."""
    files: dict[str, str] = {}
    pod_spec: dict = {}
    if rec.resources is not None:
        r = rec.resources
        limits = {"cpu": cpu_qty(r.cpu_limit_m) if r.cpu_limit_m else None, "memory": mem_qty(r.mem_limit_mi)}
        pod_spec["containers"] = [{"name": container, "resources": {
            "requests": {"cpu": cpu_qty(r.cpu_request_m), "memory": mem_qty(r.mem_request_mi)}, "limits": limits}}]

    plan = rec.scaling
    patch: dict = {}
    if plan is not None and not plan.cron_windows and plan.cpu_utilization_target is None:
        patch["replicas"] = plan.min_replicas  # fixed replica count: no autoscaler needed
    elif plan is not None:
        triggers = [{"type": "cron", "metadata": {"timezone": tz, "start": w.start, "end": w.end,
                                                  "desiredReplicas": str(w.desired_replicas)}}
                    for w in plan.cron_windows]
        if plan.cpu_utilization_target is not None:
            triggers.append({"type": "cpu", "metricType": "Utilization",
                             "metadata": {"value": str(plan.cpu_utilization_target)}})
        files[f"{name}.keda.yaml"] = _dump({
            "apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
            "metadata": {"name": name, "namespace": namespace, "labels": MANAGED_BY},
            "spec": {"scaleTargetRef": {"name": name}, "minReplicaCount": plan.min_replicas,
                     "maxReplicaCount": plan.max_replicas, "triggers": triggers}})
    if pod_spec:
        patch["template"] = {"spec": pod_spec}
    if patch:
        files[f"{name}.patch.yaml"] = _dump(deployment_patch(namespace, name, patch))
    return files


def deployment_patch(namespace: str, name: str, spec: dict) -> dict:
    """A strategic-merge patch with a full header, so it works with both `kubectl patch` and kustomize."""
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": namespace},
            "spec": spec}
