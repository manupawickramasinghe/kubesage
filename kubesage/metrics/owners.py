"""Which controller owns a pod: the join both cost allocation and right-sizing need.

kube-state-metrics exports owner references as series (`kube_pod_owner`, `kube_replicaset_owner`,
`kube_job_owner`). Following them gives the real answer: ReplicaSet -> Deployment, Job -> CronJob. A cluster that
doesn't export them still gets an answer read from the pod's name, which is a guess for anything but the common
Deployment and StatefulSet shapes, and says so through the kind 'Workload'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kubesage.metrics.client import PromClient, PromError

Key = tuple[str, str]  # (namespace, name)
Owner = tuple[str, str]  # (kind, name)

# Kubernetes builds generated name parts from an alphabet without vowels or look-alike digits, which is what
# tells "web-7d9f8c6b5-x2k9p" (a Deployment's pod) from "node-problem-detector-x2k9p" (a DaemonSet's).
_SAFE = "[bcdfghjklmnpqrstvwxz2456789]"
_HASH = rf"(?:{_SAFE}{{5,10}}|\d{{6,10}})"  # a ReplicaSet's pod-template hash; older clusters used digits
_DEPLOY_POD = re.compile(rf"(.+)-{_HASH}-{_SAFE}{{5}}")
_SET_POD = re.compile(r"(.+)-\d+")
_HASHED_POD = re.compile(rf"(.+)-{_SAFE}{{5}}")
_RS_NAME = re.compile(rf"(.+)-{_HASH}")
_JOB_NAME = re.compile(r"(.+)-\d{8,10}")


@dataclass
class Owners:
    pods: dict[Key, Owner] = field(default_factory=dict)
    replicasets: dict[Key, Owner] = field(default_factory=dict)
    jobs: dict[Key, Owner] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def resolve(namespace: str, pod: str, pods: dict[Key, Owner], replicasets: dict[Key, Owner],
            jobs: dict[Key, Owner]) -> Owner:
    """(kind, name) of the controller that owns a pod."""
    owner = pods.get((namespace, pod))
    if owner:
        kind, name = owner
        if kind == "ReplicaSet":
            parent = replicasets.get((namespace, name))
            if parent:
                return parent
            m = _RS_NAME.fullmatch(name)
            return ("Deployment", m.group(1)) if m else (kind, name)
        if kind == "Job":
            parent = jobs.get((namespace, name))
            if parent:
                return parent
            m = _JOB_NAME.fullmatch(name)
            return ("CronJob", m.group(1)) if m else (kind, name)
        return kind, name
    for pattern, kind in ((_DEPLOY_POD, "Deployment"), (_SET_POD, "StatefulSet"), (_HASHED_POD, "Workload")):
        m = pattern.fullmatch(pod)
        if m:
            return kind, m.group(1)
    return "Pod", pod


def workload_of(namespace: str, pod: str, owners: Owners) -> Owner:
    return resolve(namespace, pod, owners.pods, owners.replicasets, owners.jobs)


def fetch(client: PromClient, *, at: float, days: int, namespace: str | None = None) -> Owners:
    """Every owner reference seen in the last `days`, for one namespace or all. Missing or failing series are not
    an error: the name-based fallback covers them, and a failure is kept as a note."""
    out = Owners()
    sel = f'{{namespace="{namespace}"}}' if namespace else ""
    for metric, key, target in (("kube_pod_owner", "pod", out.pods), ("kube_replicaset_owner", "replicaset", out.replicasets),
                                ("kube_job_owner", "job_name", out.jobs)):
        try:
            rows = client.query(f"max by (namespace, {key}, owner_kind, owner_name) (last_over_time({metric}{sel}[{days}d]))", at=at)
        except PromError as exc:
            out.notes.append(f"Could not read {metric}: {exc}")
            continue
        for r in rows:
            m = r["metric"]
            if m.get("owner_name") and m.get("owner_kind") not in (None, "", "<none>"):
                target[(m.get("namespace", ""), m.get(key, ""))] = (m["owner_kind"], m["owner_name"])
    return out
