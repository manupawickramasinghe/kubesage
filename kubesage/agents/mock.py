"""Deterministic stand-ins for the three roles, used without an API key. They run on the same real signals and
inventory as the models, so mock runs show real data; only the wording and judgement are rule-based."""
from __future__ import annotations

import math

from kubesage.agents.schemas import Action, ChangeSet, Diagnosis, Finding, Issue, SubAgentReport

CATEGORY = {"memory_working_set": "memory", "oom_kills_10m": "memory", "cpu_cores": "cpu", "cpu_throttled_ratio": "cpu",
            "cpu_flat": "cpu", "disk_write_rate": "disk_io", "pressure_io_stall": "disk_io", "volume_used_ratio": "disk_space",
            "latency_p95": "latency", "pending_reason": "pod_count", "workload_pods": "pod_count", "pods_on_node": "pod_density",
            "pod_posture": "security", "not_in_gitops": "security", "namespace_no_pod_security": "security",
            "web_unknown_paths": "security", "web_csrf_rejected": "security", "web_shell_refused": "security",
            "restarts_total": "memory", "restarts_10m": "memory"}


def subagent(agent: str, signals: list[dict], max_findings: int) -> SubAgentReport:
    flagged = [s for s in signals if s.get("severity") in ("warning", "critical")]
    groups: dict[tuple[str, str], list[dict]] = {}
    for s in flagged:
        groups.setdefault((s["target"], CATEGORY.get(s["kind"], "other")), []).append(s)
    findings = []
    for (target, category), sigs in sorted(groups.items(), key=lambda g: -max(s["severity"] == "critical" for s in g[1])):
        facts = "; ".join(f"{s['kind']} {s['value']}{(' ' + s['unit']) if s['unit'] else ''}"
                          f"{(' (' + s['detail'] + ')') if s['detail'] else ''}" for s in sigs)
        findings.append(Finding(title=f"{category.replace('_', ' ')}: {target.split('/')[-1]}",
                                severity="critical" if any(s["severity"] == "critical" for s in sigs) else "warning",
                                category=category, target=target, signal_ids=[s["id"] for s in sigs], summary=facts[:600]))
    summary = (f"{len(findings)} problem(s) across {len({f.target for f in findings})} target(s)" if findings
               else f"nothing above threshold in {len(signals)} signals")
    return SubAgentReport(summary=f"{agent}: {summary} (mock: grouped by rule, no model)", findings=findings[:max_findings])


def _sig(signals: dict, target: str, kind: str):
    return next((s for s in signals.values() if s["target"].endswith(target.split("/", 1)[-1]) and s["kind"] == kind), None)


def thinker(findings: dict[str, SubAgentReport], signals: dict[str, dict], inventory: dict) -> Diagnosis:
    by_target: dict[str, set[str]] = {}
    evidence: dict[str, list[str]] = {}
    for report in findings.values():
        for f in report.findings:
            key = f.target.split("/")[-1]
            by_target.setdefault(key, set()).add(f.category)
            evidence.setdefault(key, []).extend(f.signal_ids)
    workloads = {w["name"]: w for w in inventory["workloads"]}
    pools = {p["name"]: p for p in inventory["node_pools"]}
    lab = pools.get("lab", {})
    issues, actions = [], []

    def add(category, name, title, cause, severity, acts):
        iid = f"I{len(issues) + 1}"
        issues.append(Issue(id=iid, title=title, category=category, severity=severity, confidence=0.6, root_cause=cause,
                            evidence=evidence.get(name, [])[:8], targets=[f"deployment/lab/{name}"]))
        for a in acts:
            actions.append(Action(id=f"A{len(actions) + 1}", issue=iid, **a))

    for name, cats in sorted(by_target.items()):
        w = workloads.get(name)
        if not w:
            continue
        c = (w.get("containers") or [{}])[0]
        lim = c.get("limits", {})
        dep = f"deployment/lab/{name}"
        flat = _sig(signals, dep, "cpu_flat")
        thr = _sig(signals, dep, "cpu_throttled_ratio")
        if "memory" in cats:
            new = f"{max(1024, 2 * _mib(lim.get('memory', '512Mi')))}Mi"
            add("memory", name, f"{name} runs out of memory", "Working set reaches the memory limit and the kernel OOM-kills it.",
                "critical", [dict(type="set_resources", target=dep, summary=f"Raise {name}'s memory limit to {new}",
                                  params={"container": c.get("name", name), "requests": {"memory": new}, "limits": {"memory": new}},
                                  rationale="Size memory for the observed working set plus headroom.")]
                + ([dict(type="change_node_sku", target="nodepool/lab", summary="Move the lab pool to a memory-optimised SKU",
                         params={"pool": "lab", "from_sku": lab.get("sku"), "to_sku": "E8s_v5"},
                         rationale="The compute-optimised pool has 2 GiB per core; memory-heavy workloads need E-series.")]
                   if lab.get("family") == "compute-optimised" else []))
        elif "cpu" in cats and flat and flat.get("severity"):
            src = next((f for f in w.get("sources", [])), None)
            add("cpu", name, f"{name} can only use one core", "CPU is pinned at one core with no throttling while the limit "
                "allows more: the work is serialised in one thread (Python's GIL).", "warning",
                [dict(type="code_change", target=dep, summary="Run the scoring on a process pool instead of threads",
                      params={"file": src, "goal": "use processes so the CPU-bound work runs on every granted core"},
                      rationale="A bigger SKU adds cores the code can't use.")])
        elif "cpu" in cats and thr and (thr.get("value") or 0) > 0.25:
            cur = _cores(lim.get("cpu", "1"))
            need = min(4, max(cur + 1, math.ceil(cur / max(0.05, 1 - thr["value"]))))
            add("cpu", name, f"{name} is CPU-throttled", f"It is throttled in {thr['value']:.0%} of periods at a {cur:g}-core limit.",
                "warning", [dict(type="set_resources", target=dep, summary=f"Give {name} {need} cores",
                                 params={"container": c.get("name", name), "requests": {"cpu": str(need)}, "limits": {"cpu": str(need)}},
                                 rationale="The work is parallel and wants more cores than the limit grants.")])
        if "latency" in cats:
            add("latency", name, f"{name} is slow", "Requests queue behind a CPU limit that is too small for the traffic.",
                "warning", [dict(type="set_resources", target=dep, summary=f"Raise {name}'s CPU limit to 1 core",
                                 params={"container": c.get("name", name), "limits": {"cpu": "1"}, "requests": {"cpu": "500m"}},
                                 rationale="Throttling stretches every request."),
                            dict(type="autoscale", target=dep, summary=f"Autoscale {name} on CPU with KEDA",
                                 params={"min_replicas": 1, "max_replicas": 4, "trigger": "cpu", "threshold": 60},
                                 rationale="Scale out with traffic instead of sizing one pod for the peak.")])
        if "disk_io" in cats:
            add("disk_io", name, f"{name} saturates local disk", "Many small fsynced writes keep the disk busy.", "warning",
                [dict(type="change_node_sku", target="nodepool/lab", summary="Use a storage-optimised SKU with local NVMe",
                      params={"pool": "lab", "from_sku": lab.get("sku"), "to_sku": "L8s_v3"},
                      rationale="The custom rules forbid batching the fsyncs, so the fix is faster disk.")])
        if "disk_space" in cats:
            add("disk_space", name, f"{name}'s scratch volume is nearly full", "The cache never evicts, so it grows until kubelet "
                "evicts the pod.", "warning", [dict(type="storage", target=dep, summary="Double the scratch volume's size limit",
                                                    params={"volume": "cache", "size_limit": "2Gi"},
                                                    rationale="Buys time; the cache still needs eviction.", risk="Only delays the problem.")])
        if "pod_count" in cats:
            add("pod_count", name, f"{name} has Pending pods", "Each replica requests far more CPU than it uses.", "warning",
                [dict(type="set_resources", target=dep, summary=f"Cut {name}'s CPU request to 50m",
                      params={"container": c.get("name", name), "requests": {"cpu": "50m"}},
                      rationale="The pods use almost no CPU; more nodes would cost money for nothing.")])
        if "security" in cats:
            add("security", name, f"{name} was created outside GitOps", "A bare pod with a floating tag runs as root without limits.",
                "warning", [dict(type="security_hardening", target="namespace/lab", summary="Enforce Pod Security on the lab namespace",
                                 params={"namespace": "lab", "pod_security": "baseline"}, rationale="Stops privileged specs at admission."),
                            dict(type="remove_workload", target=f"pod/lab/{name}", summary=f"Remove {name} once its owner is found",
                                 params={"kind": "Pod", "name": name}, rationale="It is not part of the desired state.",
                                 needs_human=True)])
    for name, cats in sorted(by_target.items()):
        if "pod_density" in cats and name in {n["name"] for n in inventory["nodes"]}:
            add("pod_density", name, f"{name} is close to its pod limit", "One node carries most of the pods.", "warning",
                [dict(type="set_node_pool", target="nodepool/lab", summary="Raise max_pods on the lab pool to 250",
                      params={"pool": "lab", "max_pods": 250}, rationale="The pods are tiny; the limit, not CPU, is the constraint.")])
    return Diagnosis(summary=f"{len(issues)} issue(s) (mock: rule-based, no model)", issues=issues, actions=actions)


def executor(diagnosis: Diagnosis, edits: list, skipped: list) -> ChangeSet:
    lines = [f"Automated fix from the KubeSage agent team (mock mode: rule-based, no model).", ""]
    for issue in diagnosis.issues:
        lines += [f"## {issue.title}", "", issue.root_cause, ""]
        lines += [f"- {a.summary}" for a in diagnosis.actions if a.issue == issue.id]
        lines.append("")
    if skipped:
        lines += ["## Not changed", ""] + [f"- {s.action}: {s.reason}" for s in skipped]
    title = diagnosis.issues[0].title if len(diagnosis.issues) == 1 else f"Fix {len(diagnosis.issues)} issues in the lab namespace"
    return ChangeSet(title=title[:72], body="\n".join(lines), edits=edits, skipped=skipped)


def _mib(q: str) -> int:
    q = str(q)
    return int(float(q[:-2]) * 1024) if q.endswith("Gi") else int(float(q[:-2])) if q.endswith("Mi") else 512


def _cores(q: str) -> float:
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)
