"""Copy kubelet's /stats/summary disk numbers into VictoriaMetrics, every 15 s while `kubesage serve` runs.

cAdvisor doesn't report emptyDir usage under containerd, and no exporter in the stack reads kubelet's summary
API, so without this nothing could alert on a filling scratch volume. Series written:
  kubesage_pod_volume_used_bytes{namespace,pod,volume,node}    from stats/summary
  kubesage_pod_volume_limit_bytes{namespace,pod,volume,node}   the emptyDir sizeLimit from the pod spec
  kubesage_pod_ephemeral_used_bytes{namespace,pod,node}
  kubesage_node_fs_used_bytes / kubesage_node_fs_capacity_bytes{node}
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

import httpx

from kubesage.apply import kubectl_base

NAMESPACES = ("lab", "demo")


def _get(*args: str) -> dict | None:
    try:
        proc = subprocess.run([*kubectl_base(), "--request-timeout=8s", *args], capture_output=True, text=True, timeout=12)
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _bytes(quantity: str) -> float:
    units = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for suffix in sorted(units, key=len, reverse=True):
        if quantity.endswith(suffix):
            return float(quantity[:-len(suffix)]) * units[suffix]
    return float(quantity)


def _labels(**kv: str) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in kv.items()) + "}"


def lines(nodes: dict, pods_by_ns: dict[str, dict], summaries: dict[str, dict]) -> list[str]:
    limits = {}
    for pods in pods_by_ns.values():
        for pod in (pods or {}).get("items", []):
            for vol in pod["spec"].get("volumes", []):
                size = (vol.get("emptyDir") or {}).get("sizeLimit")
                if size:
                    limits[(pod["metadata"]["namespace"], pod["metadata"]["name"], vol["name"])] = _bytes(size)
    out = []
    for node, summary in summaries.items():
        fs = summary.get("node", {}).get("fs", {})
        if fs.get("usedBytes") is not None:
            out.append(f"kubesage_node_fs_used_bytes{_labels(node=node)} {fs['usedBytes']}")
            out.append(f"kubesage_node_fs_capacity_bytes{_labels(node=node)} {fs['capacityBytes']}")
        for pod in summary.get("pods", []):
            ref = pod["podRef"]
            if ref["namespace"] not in NAMESPACES:
                continue
            base = {"namespace": ref["namespace"], "pod": ref["name"], "node": node}
            if (pod.get("ephemeral-storage") or {}).get("usedBytes") is not None:
                out.append(f"kubesage_pod_ephemeral_used_bytes{_labels(**base)} {pod['ephemeral-storage']['usedBytes']}")
            for vol in pod.get("volume", []):
                key = (ref["namespace"], ref["name"], vol["name"])
                if key in limits and vol.get("usedBytes") is not None:
                    out.append(f"kubesage_pod_volume_used_bytes{_labels(**base, volume=vol['name'])} {vol['usedBytes']}")
                    out.append(f"kubesage_pod_volume_limit_bytes{_labels(**base, volume=vol['name'])} {limits[key]:.0f}")
    return out


def export_once(vm: str) -> int:
    nodes = _get("get", "nodes", "-o", "json") or {"items": []}
    names = [n["metadata"]["name"] for n in nodes["items"]]
    summaries = {name: s for name in names if (s := _get("get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary"))}
    pods = {ns: _get("-n", ns, "get", "pods", "-o", "json") for ns in NAMESPACES}
    body = lines(nodes, pods, summaries)
    if body:
        httpx.post(f"{vm.rstrip('/')}/api/v1/import/prometheus", content="\n".join(body) + "\n", timeout=10).raise_for_status()
    return len(body)


def start(vm: str, every: float = 15.0) -> threading.Thread:
    def loop():
        while True:
            try:
                export_once(vm)
            except Exception:  # the cluster or VictoriaMetrics being down must not kill the thread
                pass
            time.sleep(every)
    thread = threading.Thread(target=loop, name="kubestats", daemon=True)
    thread.start()
    return thread
