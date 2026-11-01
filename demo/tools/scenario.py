"""The hypothetical cluster the video shows: shop-prod, an online store on Azure Kubernetes Service.

The replay data in data/ was recorded on KubeSage's two-machine lab, whose nodes stand in for one Azure D8s_v5 and one
F8s_v2: the cost model already prices them as those VMs. For the video they are shown as those VMs. This module renames
the nodes, describes their hardware as the VMs', rewrites the two platform rules that describe the lab machines, and
leaves out four lines of an agent's reasoning about the lab hardware. Everything else (pods, usage, the analysis, the
agents' findings, the commit and its diff) is the recording, unchanged.
"""
from __future__ import annotations

import re

NAMES = {"k3d-kubesage-server-0": "aks-system-vmss000000", "manupa-hnx": "aks-lab-vmss000000"}
_VM = {"os": "Ubuntu 24.04.3 LTS", "kernel": "6.8.0-1041-azure", "kubelet": "v1.36.4", "runtime": "containerd://2.3.4",
       "k3d": False, "external_ip": None, "cpu_cores": 8.0, "alloc_cpu_m": 7820}
NODES = {
    "aks-system-vmss000000": _VM | {"host": "aks-system-0", "role": "system", "cpu_model": "Xeon Platinum 8370C",
                                    "internal_ip": "10.224.0.4", "mem_mi": 32096, "alloc_mem_mi": 27852},
    "aks-lab-vmss000000": _VM | {"host": "aks-lab-0", "role": "lab", "cpu_model": "Xeon Platinum 8272CL",
                                 "internal_ip": "10.224.0.5", "mem_mi": 16012, "alloc_mem_mi": 13107},
}
TEXT = [
    ("pool system; 12 cores / 13.0 GiB allocatable; K3s v1.36.4+k3s1, kernel 7.1.5-76070105-generic",
     "pool system; 7.8 cores / 27.2 GiB allocatable; Kubernetes v1.36.4, kernel 6.8.0-1041-azure"),
    ("pool lab; 8 cores / 14.1 GiB allocatable; Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic",
     "pool lab; 7.8 cores / 12.8 GiB allocatable; Ubuntu 24.04.3 LTS, kernel 6.8.0-1041-azure"),
    ("6.79 of 12 cores requested", "6.79 of 7.82 cores requested"),
    ("9.02 of 13.0 GiB requested", "9.02 of 27.2 GiB requested"),
    ("0.25 of 14.1 GiB requested", "0.25 of 12.8 GiB requested"),
    ("is the 3500U laptop, standing in for one Azure F8s_v2 node.", "is one Azure F8s_v2 node."),
    ("The system pool is the 8600G desktop, standing in for one D8s_v5.", "The system pool is one Azure D8s_v5."),
    ("The laptop also runs Pi-hole and GitHub runners outside Kubernetes.", "The lab node also runs the platform's own agents."),
]
SIGNALS = {"resources:aks-system-vmss000000.requested_cpu": 0.8683, "resources:aks-system-vmss000000.requested_mem": 0.3316,
           "resources:aks-system-vmss000000.mem_available": 17.9, "resources:aks-lab-vmss000000.requested_mem": 0.0195}
# The resources agent's reasoning about the lab machines' own sizes: left out rather than rewritten.
DROP = re.compile(r"node aks-system-vmss000000: |node aks-lab-vmss000000: |Node k3d memory|requested memory on k3d")


def text(s: str) -> str:
    for a, b in TEXT:
        s = s.replace(a, b)
    for a, b in NAMES.items():
        s = s.replace(a, b)
    return s


def apply(o):
    """The recorded object, as it would read on shop-prod."""
    if isinstance(o, str):
        return text(o)
    if isinstance(o, list):
        return [apply(x) for x in o]
    if isinstance(o, dict):
        out = {k: apply(v) for k, v in o.items()}
        if out.get("id") in SIGNALS:
            out["value"] = SIGNALS[out["id"]]
        if "pods" in out and out.get("name") in NODES:
            out.update(NODES[out["name"]])
        return out
    return o


def keep(line: dict) -> bool:
    return not DROP.search(line["text"])
