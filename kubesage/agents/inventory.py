"""What the Thinker reasons over besides the findings: workloads, nodes, node pools and the SKU catalog."""
from __future__ import annotations

import re
from pathlib import Path

from kubesage.agents.context import Context
from kubesage.config import SkuCatalog

_REPLICA = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$")


def node_pools(tf: str) -> list[dict]:
    pools = []
    for m in re.finditer(r'(?:resource "azurerm_kubernetes_cluster_node_pool" "([\w-]+)" \{|default_node_pool \{)(.*?)\n\s*\}', tf, re.S):
        body = m.group(2)
        get = lambda key: (re.search(rf'{key}\s*=\s*"?([\w.-]+)"?', body) or [None, None])[1]
        pools.append({"name": get("name") or m.group(1), "sku": (get("vm_size") or "").removeprefix("Standard_"),
                      "node_count": int(get("node_count") or 0), "max_pods": int(get("max_pods") or 110)})
    return pools


def build(ctx: Context, gitops: Path, catalog: SkuCatalog, namespaces: list[str]) -> dict:
    workloads = []
    for ns in namespaces:
        for d in (ctx.kube("-n", ns, "get", "deployments") or {}).get("items", []):
            workloads.append(_workload(d["metadata"]["name"], ns, d["spec"]["template"]["spec"], d["spec"].get("replicas"),
                                       "Deployment", gitops))
        for p in (ctx.kube("-n", ns, "get", "pods") or {}).get("items", []):
            if not p["metadata"].get("ownerReferences"):
                workloads.append(_workload(p["metadata"]["name"], ns, p["spec"], 1, "Pod", gitops))
    nodes = []
    for n in (ctx.kube("get", "nodes") or {}).get("items", []):
        labels, info = n["metadata"].get("labels", {}), n["status"].get("nodeInfo", {})
        nodes.append({"name": n["metadata"]["name"], "pool": labels.get("kubesage.io/pool"), "stands_in_for": labels.get("kubesage.io/sku"),
                      "capacity": {k: n["status"]["capacity"].get(k) for k in ("cpu", "memory", "pods")},
                      "allocatable": {k: n["status"]["allocatable"].get(k) for k in ("cpu", "memory", "pods")},
                      "os": info.get("osImage"), "kernel": info.get("kernelVersion")})
    tf = gitops / "infra" / "nodepools.tf"
    pools = node_pools(tf.read_text()) if tf.exists() else []
    hours = catalog.hours_per_month
    for pool in pools:
        try:
            sku = catalog.get(pool["sku"])
            pool.update(family=sku.family, vcpu=sku.vcpu, memory_gib=sku.memory_gib,
                        allocatable_cores=round(sku.vcpu * catalog.allocatable_cpu * pool["node_count"], 2),
                        allocatable_memory_gib=round(sku.memory_gib * catalog.allocatable_mem * pool["node_count"], 2),
                        monthly_usd=round(sku.hourly_usd * hours * pool["node_count"], 2))
        except KeyError:
            pass
    return {"workloads": workloads, "nodes": nodes, "node_pools": pools,
            "allocatable_share": {"cpu": catalog.allocatable_cpu, "memory": catalog.allocatable_mem},
            "skus": [{"name": s.name, "family": s.family, "vcpu": s.vcpu, "memory_gib": s.memory_gib,
                      "monthly_usd": round(s.hourly_usd * hours, 2), **({"local_nvme_gib": s.local_nvme_gib} if s.local_nvme_gib else {})}
                     for s in catalog.skus]}


def _workload(name: str, ns: str, spec: dict, replicas, kind: str, gitops: Path) -> dict:
    app_dir = gitops / "apps" / name
    path = app_dir / "deployment.yaml"
    return {"name": name, "namespace": ns, "kind": kind, "replicas": replicas,
            "node_selector": spec.get("nodeSelector"),
            "containers": [{"name": c["name"], "image": c.get("image"), "requests": c.get("resources", {}).get("requests", {}),
                            "limits": c.get("resources", {}).get("limits", {}),
                            "env": {e["name"]: e.get("value") for e in c.get("env", []) if "value" in e}}
                           for c in spec.get("containers", [])],
            "gitops": f"gitops/apps/{name}/deployment.yaml" if path.exists() else None,
            "sources": sorted(f"gitops/apps/{name}/src/{p.name}" for p in (app_dir / "src").glob("*.py")) if app_dir.exists() else [],
            # The code itself, so the Thinker can tell "needs more cores" from "can't use the cores it has".
            "source_code": {f"gitops/apps/{name}/src/{p.name}": p.read_text()[:8000]
                            for p in sorted((app_dir / "src").glob("*.py"))} if app_dir.exists() else {}}
