"""What else is on the cluster: the pods a right-sizing run does not touch, and the nodes it runs on.

A node pool has to hold every pod, not only the namespace being right-sized. Without this, a plan for one
namespace quietly assumes the cluster is empty apart from it. `inventory` reads, at the newest moment the
database has samples for, every pod's requests and the nodes' SKUs; `background` turns that into the demand the
analysis must leave room for: other namespaces, DaemonSets and Jobs, and the sidecars of the workloads being
resized.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from kubesage.config import NodePool, SkuCatalog
from kubesage.metrics import owners as own
from kubesage.metrics.client import PromClient, PromError
from kubesage.metrics.stats import WorkloadStats

MIB = 2**20
# The last ten minutes, not the instant: a scrape interval of a few minutes must not make a running pod vanish.
REQUESTS = 'last_over_time(kube_pod_container_resource_requests{container!="", container!="POD"}[10m])'


@dataclass
class Inventory:
    at: float | None = None  # when the pods and nodes were read; None: the database has no pod inventory
    pods: dict[tuple[str, str], dict[str, tuple[float, float]]] = field(default_factory=dict)  # container -> (cores, MiB)
    owners: own.Owners = field(default_factory=own.Owners)
    pool: NodePool | None = None  # the nodes as one SKU and a count, when they are that simple
    notes: list[str] = field(default_factory=list)


def _newest(client: PromClient, end: float, days: int) -> float | None:
    """The latest moment pod requests were being scraped: now on a live cluster, the end of the history on a
    simulated one. Looked for hour by hour, so one cheap series instead of every pod's."""
    if client.query(f"count({REQUESTS})", at=end):
        return end
    end = int(end) // 3600 * 3600
    rows = client.query_range(f"count({REQUESTS})", end - days * 86400, end, 3600)
    stamps = [float(ts) for r in rows for ts, v in r["values"] if float(v) > 0]
    return max(stamps) if stamps else None


def inventory(client: PromClient, *, catalog: SkuCatalog, end: float, days: int) -> Inventory:
    from kubesage import costs  # node pricing lives with cost allocation; imported here to keep the modules acyclic

    inv = Inventory(owners=own.fetch(client, at=end, days=days))
    inv.notes += inv.owners.notes
    inv.at = at = _newest(client, end, days)
    if at is None:
        inv.notes.append("The database has no pod inventory (kube_pod_container_resource_requests) for the window: "
                         "the node pool is sized for the analysed namespaces alone.")
        return inv
    finished: set[tuple[str, str]] = set()
    try:  # a finished pod still exports its requests until it is deleted; it holds nothing on a node
        for r in client.query('max by (namespace, pod) (kube_pod_status_phase{phase=~"Succeeded|Failed"}) == 1', at=at):
            finished.add((r["metric"].get("namespace", ""), r["metric"].get("pod", "")))
    except PromError:
        pass
    for r in client.query(f"max by (namespace, pod, container, resource) ({REQUESTS})", at=at):
        m = r["metric"]
        key = (m.get("namespace", ""), m.get("pod", ""))
        if key in finished or not key[1]:
            continue
        cores, mib = inv.pods.setdefault(key, {}).get(m.get("container", ""), (0.0, 0.0))
        value = float(r["value"][1])
        inv.pods[key][m.get("container", "")] = (value, mib) if m.get("resource") == "cpu" else (cores, value / MIB)

    try:
        names = sorted(r["metric"].get("node", "") for r in client.query("count by (node) (kube_node_info)", at=at))
        labels = (costs._label(catalog.node_label), "label_node_kubernetes_io_instance_type")
        skus = {r["metric"].get("node", ""): next((r["metric"][k] for k in labels if r["metric"].get(k)), None)
                for r in client.query(f"max by (node, {', '.join(labels)}) (kube_node_labels)", at=at)}
        capacity: dict[str, dict[str, float]] = {}
        for r in client.query('max by (node, resource) (kube_node_status_capacity{resource=~"cpu|memory"})', at=at):
            capacity.setdefault(r["metric"].get("node", ""), {})[r["metric"].get("resource")] = float(r["value"][1])
    except PromError as exc:
        inv.notes.append(f"Could not read the nodes: {exc}")
        return inv
    if not names:
        return inv
    raw = costs.RawCosts(days=1, end=at, nodes=[
        costs.Node(name=n, hours=np.array([24.0]), sku=skus.get(n), cpu=capacity.get(n, {}).get("cpu"),
                   mem_gib=capacity[n]["memory"] / costs.GIB if "memory" in capacity.get(n, {}) else None) for n in names])
    lines = costs._price_nodes(raw, catalog, np.array([True]))[4]
    known = {line.sku for line in lines if line.priced_by in ("label", "capacity")}
    if len(known) == 1 and all(line.priced_by in ("label", "capacity") for line in lines):
        inv.pool = NodePool(sku=known.pop(), nodes=len(lines))
    else:
        mix = ", ".join(sorted({f"{line.sku}" for line in lines}))
        inv.notes.append(f"The cluster's {len(lines)} nodes are not one catalog SKU ({mix}): the plan is compared with "
                         f"the pool in config/skus.yaml, {catalog.current_pool.nodes}× {catalog.current_pool.sku}.")
    return inv


def background(inv: Inventory, stats: list[WorkloadStats]) -> list[tuple[float, float]]:
    """(cores, MiB) requested by each pod, or part of a pod, that the analysis leaves alone."""
    resized = {(s.namespace, s.kind, s.name): s.container or s.name for s in stats}
    out = []
    for (namespace, pod), containers in sorted(inv.pods.items()):
        main = resized.get((namespace, *own.workload_of(namespace, pod, inv.owners)))
        cores = sum(c for name, (c, _) in containers.items() if name != main)
        mib = sum(m for name, (_, m) in containers.items() if name != main)
        if cores > 0 or mib > 0:
            out.append((round(cores, 4), round(mib, 1)))
    return out


def namespaces(client: PromClient, *, end: float, days: int) -> list[str]:
    """Every namespace that had a running container in the window."""
    rows = client.query('count by (namespace) (last_over_time(container_memory_working_set_bytes{container!="", '
                        f'container!="POD"}}[{days}d]))', at=end)
    return sorted(r["metric"]["namespace"] for r in rows if r["metric"].get("namespace"))
