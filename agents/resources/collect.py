"""System-resources sub-agent: nodes, scheduling, volumes and warning events from the API and kubelet."""
import re
from datetime import datetime, timezone

_REPLICA = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$")
_CPU = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
_MEM = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "k": 1e3, "M": 1e6, "G": 1e9}


def cores(q):
    q = str(q)
    return float(q[:-1]) * _CPU[q[-1]] if q[-1] in _CPU else float(q)


def nbytes(q):
    q = str(q)
    for s in sorted(_MEM, key=len, reverse=True):
        if q.endswith(s):
            return float(q[:-len(s)]) * _MEM[s]
    return float(q)


def owner(pod):
    refs = pod["metadata"].get("ownerReferences") or []
    return _REPLICA.sub("", pod["metadata"]["name"]) if refs else pod["metadata"]["name"]


def collect(ctx):
    nodes = (ctx.kube("get", "nodes") or {}).get("items", [])
    all_pods = (ctx.kube("get", "pods", "-A") or {}).get("items", [])
    on_node = {}
    for p in all_pods:
        if p["spec"].get("nodeName") and p.get("status", {}).get("phase") not in ("Succeeded", "Failed"):
            on_node.setdefault(p["spec"]["nodeName"], []).append(p)
    for n in nodes:
        name, labels = n["metadata"]["name"], n["metadata"].get("labels", {})
        alloc, info = n["status"]["allocatable"], n["status"].get("nodeInfo", {})
        pods = on_node.get(name, [])
        req_cpu = sum(cores(c.get("resources", {}).get("requests", {}).get("cpu", 0)) for p in pods for c in p["spec"]["containers"])
        req_mem = sum(nbytes(c.get("resources", {}).get("requests", {}).get("memory", 0)) for p in pods for c in p["spec"]["containers"])
        target = f"node/{name}"
        pool = labels.get("kubesage.io/pool", "-")
        ctx.signal(f"{name}.node", "node", target, labels.get("kubesage.io/sku", "-"),
                   detail=f"pool {pool}; {alloc['cpu']} cores / {nbytes(alloc['memory']) / 2 ** 30:.1f} GiB allocatable; "
                          f"{info.get('osImage', '')}, kernel {info.get('kernelVersion', '')}")
        ctx.signal(f"{name}.requested_cpu", "requested_cpu_ratio", target, req_cpu / cores(alloc["cpu"]), unit="ratio",
                   detail=f"{req_cpu:.2f} of {cores(alloc['cpu']):g} cores requested",
                   severity="warning" if req_cpu > 0.9 * cores(alloc["cpu"]) else None)
        ctx.signal(f"{name}.requested_mem", "requested_memory_ratio", target, req_mem / nbytes(alloc["memory"]), unit="ratio",
                   detail=f"{req_mem / 2 ** 30:.2f} of {nbytes(alloc['memory']) / 2 ** 30:.1f} GiB requested")
        density = len(pods) / int(alloc.get("pods", 110))
        ctx.signal(f"{name}.pods", "pods_on_node", target, len(pods), unit="pods",
                   detail=f"{density:.0%} of max-pods {alloc.get('pods', 110)}",
                   severity="warning" if density > 0.6 else None)
        for cond in n["status"].get("conditions", []):
            if cond["type"] != "Ready" and cond["status"] == "True":
                ctx.signal(f"{name}.{cond['type']}", "node_condition", target, cond["type"], detail=cond.get("message", ""),
                           severity="critical")
        summary = ctx.kube_raw(f"/api/v1/nodes/{name}/proxy/stats/summary") or {}
        fs = summary.get("node", {}).get("fs", {})
        if fs.get("capacityBytes"):
            ratio = fs["usedBytes"] / fs["capacityBytes"]
            ctx.signal(f"{name}.fs", "node_disk_used_ratio", target, ratio, unit="ratio",
                       detail=f"{fs['usedBytes'] / 2 ** 30:.0f} of {fs['capacityBytes'] / 2 ** 30:.0f} GiB",
                       severity="warning" if ratio > 0.85 else None)
        mem = summary.get("node", {}).get("memory", {})
        if mem.get("availableBytes") is not None:
            ctx.signal(f"{name}.mem_available", "node_memory_available", target, round(mem["availableBytes"] / 2 ** 30, 2),
                       unit="GiB")
        specs = {(p["metadata"]["namespace"], p["metadata"]["name"]): p for p in pods}
        for ps in summary.get("pods", []):
            ref = ps["podRef"]
            spec = specs.get((ref["namespace"], ref["name"]))
            if ref["namespace"] not in ctx.namespaces or not spec:
                continue
            limits = {v["name"]: nbytes(v["emptyDir"]["sizeLimit"]) for v in spec["spec"].get("volumes", [])
                      if (v.get("emptyDir") or {}).get("sizeLimit")}
            for vol in ps.get("volume", []):
                if vol["name"] in limits and vol.get("usedBytes") is not None:
                    ratio = vol["usedBytes"] / limits[vol["name"]]
                    ctx.signal(f"{owner(spec)}.{vol['name']}.volume", "volume_used_ratio",
                               f"deployment/{ref['namespace']}/{owner(spec)}", ratio, unit="ratio",
                               detail=f"emptyDir {vol['name']}: {vol['usedBytes'] / 2 ** 20:.0f} of "
                                      f"{limits[vol['name']] / 2 ** 20:.0f} MiB sizeLimit; kubelet evicts the pod above it",
                               severity="warning" if ratio > 0.85 else None)

    pods = [p for p in all_pods if p["metadata"]["namespace"] in ctx.namespaces]
    groups = {}
    for p in pods:
        groups.setdefault((p["metadata"]["namespace"], owner(p)), []).append(p)
    for (namespace, name), items in sorted(groups.items()):
        target = f"deployment/{namespace}/{name}"
        phases = {}
        for p in items:
            phase = p.get("status", {}).get("phase", "Unknown")
            phases[phase] = phases.get(phase, 0) + 1
        c0 = items[0]["spec"]["containers"][0]
        res = c0.get("resources", {})
        ctx.signal(f"{name}.pods", "workload_pods", target, len(items), unit="pods",
                   detail=", ".join(f"{k} {v}" for k, v in sorted(phases.items())) +
                          f"; per pod requests {res.get('requests', {})} limits {res.get('limits', {})}; "
                          f"node(s) {sorted({p['spec'].get('nodeName') or '-' for p in items})}",
                   severity="warning" if phases.get("Pending") else None)
        pending = [p for p in items if p.get("status", {}).get("phase") == "Pending"]
        for cond in (pending[0].get("status", {}).get("conditions", []) if pending else []):
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                ctx.signal(f"{name}.unschedulable", "pending_reason", target, len(pending), unit="pods",
                           detail=cond.get("message", ""), severity="warning")
        reasons = {}
        restarts = 0
        for p in items:
            for cs in p.get("status", {}).get("containerStatuses", []):
                restarts += cs.get("restartCount", 0)
                reason = (cs.get("lastState", {}).get("terminated") or {}).get("reason")
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
                waiting = (cs.get("state", {}).get("waiting") or {}).get("reason")
                if waiting:
                    reasons[waiting] = reasons.get(waiting, 0) + 1
        if restarts or reasons:
            ctx.signal(f"{name}.restarts", "restarts_total", target, restarts, unit="restarts",
                       detail=f"last terminations / waiting: {reasons}",
                       severity="critical" if "OOMKilled" in reasons else "warning" if restarts else None)

    cutoff = datetime.now(timezone.utc).timestamp() - 600
    live = {name for (_, name) in groups}  # events about workloads that are gone are history, not a problem
    counts = {}
    for namespace in ctx.namespaces:
        for e in (ctx.kube("-n", namespace, "get", "events") or {}).get("items", []):
            if e.get("type") != "Warning":
                continue
            at = e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp")
            try:
                if datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() < cutoff:
                    continue
            except (AttributeError, ValueError):
                continue
            obj = e.get("involvedObject", {})
            key = (namespace, e.get("reason"), _REPLICA.sub("", obj.get("name", "")))
            if key[2] not in live:
                continue
            entry = counts.setdefault(key, [0, e.get("message", "")])
            entry[0] += e.get("count") or 1
    for (namespace, reason, name), (count, message) in sorted(counts.items()):
        ctx.signal(f"{name}.event.{reason}", "warning_event", f"deployment/{namespace}/{name}", count, unit="events",
                   detail=f"{reason}: {message[:240]}", severity="warning")
    ctx.log(f"{len(ctx.signals)} signals from {len(ctx.calls)} API calls")
