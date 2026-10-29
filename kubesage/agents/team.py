"""Run the agent team once: the sub-agents in parallel, then the Thinker, then the Executor.

Every model reply is validated against the data it was given. Findings must cite collected signals, actions
must name real issues and targets, and edits must apply exactly and leave valid files. Whatever fails is
dropped and listed in the run, never silently kept. Output streams line by line into the RAM ring
(stream agent/<name>), which is what the tmux view and `kubesage ring tail` show.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kubesage import ring
from kubesage.agents import edits as E
from kubesage.agents import inventory, mock, policy, repo
from kubesage.agents.context import Context
from kubesage.agents.loader import Agent, Team
from kubesage.agents.schemas import Action, ChangeSet, Diagnosis, Edit, Skipped, SubAgentReport
from kubesage.config import load_skus
from kubesage.journal import log
from kubesage.llm.client import LLMClient, LLMError
from kubesage.llm.prompts import _schema

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = re.compile(r"(deployment|pod)/[a-z0-9-]+/[a-z0-9.-]+|(nodepool|namespace)/[a-z0-9-]+")


@dataclass
class TeamSettings:
    vm: str
    alertmanager: str
    out_dir: Path
    mode: str = "auto"
    namespaces: list[str] = field(default_factory=lambda: ["lab"])
    gitops_dir: Path = REPO_ROOT / "gitops"
    access_log: Callable[[], list[dict]] | None = None
    repo_url: str | None = None      # the Executor clones this; default: this checkout's origin
    base_branch: str | None = None   # default: KUBESAGE_AGENTS_BASE, else the executor's settings.base_branch
    open_pr: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _say(agent: str, text: str) -> None:
    ring.emit(f"agent/{agent}", text)


def contract(role: str, agent: Agent) -> str:
    if role == "subagent":
        return f"""

Reply with one JSON object only, matching this JSON schema:
{_schema(SubAgentReport)}
- Every signal_ids entry must be an id from the signals you were given. A finding without one is dropped.
- At most {agent.config.max_findings} findings, most severe first. An empty list is a valid answer."""
    if role == "thinker":
        return f"""

Reply with one JSON object only, matching this JSON schema:
{_schema(Diagnosis)}
- evidence must list signal ids from the input; an issue without valid evidence is dropped.
- At most {agent.config.settings.get('max_issues', 6)} issues. Every action's issue must be one of yours, and its
  target must exist in the inventory (deployment/<namespace>/<name>, pod/<namespace>/<name>, nodepool/<name>,
  namespace/<name>). change_node_sku's to_sku must be a catalog SKU."""
    return f"""

Reply with one JSON object only, matching this JSON schema:
{_schema(ChangeSet)}
- Each edit's find is copied exactly from the file text you were given and occurs in it exactly once.
- Every edit names the Action id it implements. Paths are repo-relative and start with gitops/."""


class TeamRun:
    def __init__(self, team: Team, trigger: str, mode: str):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._lock = threading.Lock()
        self.data = {
            "id": self.id, "trigger": trigger, "state": "running", "mode": mode, "profile": team.profile,
            "started_at": _now(), "finished_at": None, "error": None, "warning": None, "cost_usd": 0.0,
            "order": [a.name for a in team.agents],
            "agents": {a.name: {"title": a.config.title or a.name, "role": a.config.role, "size": a.config.model,
                                "label": a.tier.label or a.tier.model, "model": a.tier.model,
                                "description": a.config.description, "state": "queued", "ms": None, "signals": None,
                                "findings": None, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "error": None,
                                "summary": None} for a in team.agents},
            "rules": [name for name, _ in team.rules], "reports": {}, "signals": {}, "diagnosis": None,
            "rejected": [], "executor": None, "ledger": [],
        }

    def agent(self, name: str, **kv) -> None:
        with self._lock:
            self.data["agents"][name].update(kv)

    def set(self, **kv) -> None:
        with self._lock:
            self.data.update(kv)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.data, default=str))


def resolve_mode(team: Team, mode: str) -> str:
    return mode if mode != "auto" else ("live" if os.environ.get(team.provider.api_key_env) else "mock")


def run(team: Team, s: TeamSettings, *, trigger: str = "manual", on_update: Callable[[TeamRun], None] | None = None,
        prepared: TeamRun | None = None) -> TeamRun:
    models = team.models()
    mode = resolve_mode(team, s.mode)
    tr = prepared or TeamRun(team, trigger, mode)
    tr.set(mode=mode)
    notify = (lambda: on_update(tr)) if on_update else (lambda: None)
    run_dir = s.out_dir / tr.id
    run_dir.mkdir(parents=True, exist_ok=True)
    trace: list[dict] = []
    catalog = load_skus()
    _say("team", f"━━ team run {tr.id} ({trigger}) · {mode} · profile {team.profile} · "
                 f"{len(team.subagents)} sub-agents → thinker → executor")
    log("agents", f"team run {tr.id} started ({trigger}, {mode})")
    try:
        llm = LLMClient(models, mode)
    except LLMError as exc:
        tr.set(state="failed", error=str(exc), finished_at=_now())
        notify()
        return tr

    def context(agent: Agent) -> Context:
        return Context(agent=agent.name, settings=agent.config.settings, vm=s.vm, alertmanager=s.alertmanager,
                       namespaces=s.namespaces, gitops_dir=s.gitops_dir, access_log=s.access_log, catalog=catalog)

    def charge(name: str, entry) -> None:
        a = tr.data["agents"][name]
        tr.agent(name, cost_usd=round(a["cost_usd"] + entry.cost_usd, 6), tokens_in=a["tokens_in"] + entry.input_tokens,
                 tokens_out=a["tokens_out"] + entry.output_tokens)
        with tr._lock:
            tr.data["ledger"].append(entry.model_dump())
            tr.data["cost_usd"] = round(tr.data["cost_usd"] + entry.cost_usd, 6)
        _say(name, f"↳ {entry.model}{' (mock)' if entry.mock else ''}: {entry.input_tokens:,} in / "
                   f"{entry.output_tokens:,} out tokens · ${entry.cost_usd:.4f} · {entry.latency_ms / 1000:.1f} s")
        reasoning = _reasoning(llm.trace, name)
        if reasoning:
            for line in reasoning.splitlines()[-40:]:
                if line.strip():
                    _say(name, f"  ⋯ {line.strip()[:220]}")

    # ---- 1. sub-agents, in parallel ----
    def sub(agent: Agent) -> tuple[str, list[dict], SubAgentReport | None]:
        started = time.monotonic()
        tr.agent(agent.name, state="running", started=_now())
        notify()
        _say(agent.name, f"▶ {agent.config.title or agent.name}: collecting ({agent.config.description})")
        ctx = context(agent)
        try:
            agent.collect(ctx)
        except Exception as exc:  # a collector bug must not take the team down
            ctx.signal("collector_error", "collector_error", f"agent/{agent.name}", type(exc).__name__, detail=str(exc)[:300],
                       severity="warning")
        trace.extend(ctx.trace())
        signals = ctx.signals
        flagged = sum(1 for x in signals if x.get("severity"))
        _say(agent.name, f"  collected {len(signals)} signals ({flagged} over threshold) with {len(ctx.trace())} reads "
                         f"in {time.monotonic() - started:.1f} s")
        for x in signals:
            if x.get("severity"):
                _say(agent.name, f"  [{x['severity']}] {x['target']} {x['kind']}={x['value']}{x['unit'] and ' ' + x['unit']} "
                                 f"{x['detail'][:140]}")
        tr.agent(agent.name, signals=len(signals))
        with tr._lock:
            tr.data["signals"][agent.name] = signals
        user = json.dumps({"agent": agent.name, "now": _now(), "namespaces": ctx.namespaces, "signals": signals},
                          separators=(",", ":"))
        try:
            report, entry = llm.structured(agent.name, agent.prompt + contract("subagent", agent), user, SubAgentReport,
                                           lambda: mock.subagent(agent.name, signals, agent.config.max_findings),
                                           stage="subagent", workload=agent.name, unit="agents", flow_stage=2)
        except LLMError as exc:
            tr.agent(agent.name, state="failed", error=str(exc)[:300], ms=int((time.monotonic() - started) * 1000))
            _say(agent.name, f"✗ model call failed: {exc}")
            notify()
            return agent.name, signals, None
        charge(agent.name, entry)
        ids = {x["id"] for x in signals}
        kept = []
        for f in report.findings[:agent.config.max_findings]:
            f.signal_ids = [i for i in f.signal_ids if i in ids]
            if f.signal_ids:
                kept.append(f)
            else:
                tr.data["rejected"].append({"agent": agent.name, "what": f"finding {f.title!r}", "reason": "cites no collected signal"})
        report.findings = kept
        for f in kept:
            _say(agent.name, f"  ◆ [{f.severity}] {f.title}: {f.summary[:200]}")
        _say(agent.name, f"✓ {report.summary[:200]}")
        tr.agent(agent.name, state="done", findings=len(kept), summary=report.summary[:300],
                 ms=int((time.monotonic() - started) * 1000))
        with tr._lock:
            tr.data["reports"][agent.name] = report.model_dump()
        notify()
        return agent.name, signals, report

    with ThreadPoolExecutor(max_workers=max(1, len(team.subagents))) as pool:
        results = list(pool.map(sub, team.subagents))
    reports = {name: r for name, _, r in results if r is not None}
    all_signals = {x["id"]: x for _, sigs, _ in results for x in sigs}

    # ---- 2. Thinker ----
    thinker = team.thinker
    started = time.monotonic()
    tr.agent(thinker.name, state="running", started=_now())
    notify()
    tctx = context(thinker)
    inv = inventory.build(tctx, s.gitops_dir, catalog, s.namespaces)
    trace.extend(tctx.trace())
    cited = {i for r in reports.values() for f in r.findings for i in f.signal_ids}
    evidence = [x for x in all_signals.values() if x["id"] in cited or x.get("severity")]
    n_findings = sum(len(r.findings) for r in reports.values())
    _say(thinker.name, f"▶ Thinker: {n_findings} findings from {len(reports)} sub-agents, {len(evidence)} signals, "
                       f"{len(inv['workloads'])} workloads, {len(team.rules)} rule file(s)")
    user = json.dumps({"rules": team.rules_text(), "findings": {k: v.model_dump() for k, v in reports.items()},
                       "signals": evidence, "inventory": inv}, separators=(",", ":"))
    try:
        diagnosis, entry = llm.structured(thinker.name, thinker.prompt + contract("thinker", thinker), user, Diagnosis,
                                          lambda: mock.thinker(reports, all_signals, inv),
                                          stage="thinker", workload="team", unit="agents", flow_stage=3)
    except LLMError as exc:
        tr.agent(thinker.name, state="failed", error=str(exc)[:300])
        tr.set(state="failed", error=f"Thinker: {exc}", finished_at=_now())
        _say(thinker.name, f"✗ model call failed: {exc}")
        _finish(tr, run_dir, trace + llm.trace)
        notify()
        return tr
    charge(thinker.name, entry)
    max_issues = thinker.config.settings.get("max_issues", 6)
    before = len(tr.data["rejected"])
    checked = _check_diagnosis(diagnosis.model_copy(deep=True), all_signals, inv, catalog, tr, max_issues, s.namespaces)
    refused = [r for r in tr.data["rejected"][before:] if r["what"].startswith("action")]
    if refused and mode == "live":
        _say(thinker.name, f"  ↺ {len(refused)} action(s) failed the guardrails; asking the Thinker once more")
        for r in refused:
            _say(thinker.name, f"    ✗ {r['what']}: {r['reason'][:200]}")
        retry = (user[:-1] + "," + json.dumps({"rejected_last_time": refused}, separators=(",", ":"))[1:]
                 if user.endswith("}") else user)
        try:
            again, entry = llm.structured(thinker.name, thinker.prompt + contract("thinker", thinker) +
                                          "\n- rejected_last_time lists your previous actions that failed KubeSage's guardrails "
                                          "and why. Return the full corrected diagnosis.", retry, Diagnosis,
                                          lambda: diagnosis, stage="thinker", workload="team-retry", unit="agents", flow_stage=3)
            charge(thinker.name, entry)
            checked = _check_diagnosis(again, all_signals, inv, catalog, tr, max_issues, s.namespaces)
        except LLMError as exc:
            _say(thinker.name, f"  retry failed ({exc}); keeping the actions that passed")
    diagnosis = checked
    for issue in diagnosis.issues:
        _say(thinker.name, f"  ● {issue.id} [{issue.severity}, {issue.confidence:.0%} sure] {issue.title}: {issue.root_cause[:220]}")
    for a in diagnosis.actions:
        _say(thinker.name, f"  → {a.id} {a.type} {a.target}: {a.summary[:200]}{'  (needs a human)' if a.needs_human else ''}")
    _say(thinker.name, f"✓ {diagnosis.summary[:240]}")
    tr.agent(thinker.name, state="done", findings=len(diagnosis.issues), summary=diagnosis.summary[:300],
             ms=int((time.monotonic() - started) * 1000))
    tr.set(diagnosis=diagnosis.model_dump())
    notify()

    # ---- 3. Executor ----
    _execute(team, s, tr, llm, diagnosis, inv, catalog, run_dir, charge, notify)
    tr.set(**_outcome(tr, team), finished_at=_now())
    _say("team", f"━━ team run {tr.id} {tr.data['state']} · ${tr.data['cost_usd']:.4f} · "
                 f"{len(diagnosis.issues)} issues, {len(diagnosis.actions)} actions"
                 f"{' · branch ' + tr.data['executor']['branch'] if (tr.data.get('executor') or {}).get('commit') else ''}")
    log("agents", f"team run {tr.id} {tr.data['state']}: {len(diagnosis.issues)} issues, ${tr.data['cost_usd']:.4f}")
    _finish(tr, run_dir, trace + llm.trace)
    notify()
    return tr


def _outcome(tr: TeamRun, team: Team) -> dict:
    """The run's state from every role: a failed Executor fails the run; a failed sub-agent leaves a warning,
    because the Thinker still decided on the other reports and on every collected signal."""
    if tr.data["state"] != "running":
        return {}
    agents = tr.data["agents"]
    ex = agents[team.executor.name]
    if ex["state"] == "failed":
        return {"state": "failed", "error": f"Executor: {ex['error'] or 'failed'}"}
    subs = [a.name for a in team.subagents if agents[a.name]["state"] == "failed"]
    if subs:
        return {"state": "done", "warning": f"{len(subs)} of {len(team.subagents)} sub-agents failed ({', '.join(subs)}); "
                                            f"the Thinker decided without their reports"}
    return {"state": "done"}


def _reasoning(trace: list[dict], name: str) -> str:
    for x in reversed(trace):
        if x.get("tier") == name and x.get("sent"):
            try:
                return x["response"]["body"]["choices"][0]["message"].get("reasoning") or ""
            except (KeyError, IndexError, TypeError):
                return ""
    return ""


# What each action type may point at; anything else is a malformed action, not a judgement call.
TARGET_KINDS = {"set_resources": ("deployment", "pod"), "autoscale": ("deployment",), "code_change": ("deployment", "pod"),
                "storage": ("deployment", "pod"), "spread_pods": ("deployment",), "remove_workload": ("deployment", "pod"),
                "change_node_sku": ("nodepool",), "set_node_pool": ("nodepool",),
                "security_hardening": ("namespace", "deployment", "pod"),
                "investigate": ("deployment", "pod", "nodepool", "namespace")}


def _check_diagnosis(d: Diagnosis, signals: dict, inv: dict, catalog, tr: TeamRun, max_issues: int = 6,
                     namespaces: list[str] = ("lab",)) -> Diagnosis:
    reject = lambda what, why: tr.data["rejected"].append({"agent": "thinker", "what": what, "reason": why})
    rank = {"critical": 0, "warning": 1, "info": 2}
    ordered = sorted(d.issues, key=lambda i: rank[i.severity])
    for issue in ordered[max_issues:]:
        reject(f"issue {issue.id} {issue.title!r}", f"over the limit of {max_issues} issues per run")
    issues = []
    for issue in ordered[:max_issues]:
        issue.evidence = [i for i in issue.evidence if i in signals]
        if issue.evidence:
            issues.append(issue)
        else:
            reject(f"issue {issue.id} {issue.title!r}", "cites no collected signal")
    names = {(w["namespace"], w["name"]) for w in inv["workloads"]}
    pools = {p["name"]: p for p in inv["node_pools"]}
    skus = {x.name: x for x in catalog.skus}
    ids = {i.id for i in issues}
    actions = []
    for a in d.actions:
        why = None
        if a.issue not in ids:
            why = f"its issue {a.issue} was dropped or doesn't exist"
        elif not TARGET.fullmatch(a.target):
            why = f"target {a.target!r} is not kind/namespace/name"
        elif a.target.split("/")[0] not in TARGET_KINDS[a.type]:
            why = f"{a.type} can't target a {a.target.split('/')[0]}; it takes {' or '.join(TARGET_KINDS[a.type])}"
        elif a.target.split("/")[0] in ("deployment", "pod", "namespace") and a.target.split("/")[1] not in namespaces:
            why = f"{a.target} is outside the {', '.join(namespaces)} namespace KubeSage may change"
        elif a.target.split("/")[0] in ("deployment", "pod") and tuple(a.target.split("/")[1:]) not in names:
            why = f"{a.target} is not in the inventory"
        elif a.type in ("change_node_sku", "set_node_pool"):
            pool = (a.params or {}).get("pool") or a.target.split("/")[-1]
            if pool not in pools:
                why = f"node pool {pool!r} is not in gitops/infra/nodepools.tf"
            elif a.type == "change_node_sku":
                to = str((a.params or {}).get("to_sku", "")).removeprefix("Standard_")
                if to not in skus:
                    why = f"SKU {to!r} is not in the catalog"
                else:
                    cur = pools[pool]
                    a.params = {**a.params, "pool": pool, "from_sku": cur["sku"], "to_sku": to,
                                "monthly_delta_usd": round((skus[to].hourly_usd - skus[cur["sku"]].hourly_usd)
                                                           * catalog.hours_per_month * max(1, cur["node_count"]), 2)}
        if not why and a.type == "set_resources":
            why = _fits_today(a, inv)
        if a.type == "remove_workload":
            a.needs_human = True
        if why:
            reject(f"action {a.id} {a.type} {a.target}", why)
        else:
            actions.append(a)
    d.issues, d.actions = issues, actions
    return d


def _fits_today(a: Action, inv: dict) -> str | None:
    """Requests and limits that are real, positive quantities, request no more than the limit, and fit a node of the
    workload's pool today (a request no node can satisfy leaves the pod Pending once applied)."""
    _, ns, name = a.target.split("/")
    w = next((x for x in inv["workloads"] if (x["namespace"], x["name"]) == (ns, name)), None)
    pool = ((w or {}).get("node_selector") or {}).get("kubesage.io/pool")
    room = policy.biggest_node(inv["nodes"], pool)
    params = a.params or {}
    try:
        for kind, effect in (("requests", "the pod would stay Pending"), ("limits", "the node can't give it that much")):
            values = params.get(kind) or {}
            if not isinstance(values, dict):
                return f"{kind} must be a mapping like {{cpu: 500m, memory: 1Gi}}"
            for key, label in (("memory", "GiB"), ("cpu", "cores")):
                if values.get(key) is not None and room and policy.qty(values[key]) > room[key]:
                    have = room[key] / 2 ** 30 if key == "memory" else room[key]
                    return (f"{kind} {key}={values[key]}, but the biggest {pool or 'cluster'} node has {have:.1f} {label} "
                            f"allocatable today, so {effect}. Size requests and limits for today's nodes; put the "
                            f"future peak in a change_node_sku action instead.")
    except ValueError:
        return f"resources {params} are not valid Kubernetes quantities"
    merged_requests = {k: v for k, v in (params.get("requests") or {}).items() if v is not None}
    try:
        need = {k: policy.qty(merged_requests[k]) if k in merged_requests else 0.0 for k in ("cpu", "memory")}
    except ValueError:
        return f"resources {params} are not valid Kubernetes quantities"
    room_all = policy.rooms(inv["nodes"], pool)
    if room_all and not policy.fits(need, room_all):  # each fits some node, but not both on the same one
        return (f"requests {merged_requests} don't fit together on any one {pool or 'cluster'} node today, so the pod "
                f"would stay Pending")
    # Judge the result, not just the change: a new request is compared with the limit the container keeps.
    current = ((w or {}).get("containers") or [{}])[0]
    merged = {kind: {**(current.get(kind) or {}), **{k: v for k, v in (params.get(kind) or {}).items() if v is not None}}
              for kind in ("requests", "limits")}
    problems = policy.resources(a.target, merged)
    return problems[0] if problems else None


def _scope(a: Action) -> tuple[str, ...]:
    """The files an action may change: its own app's folder, the node pools or the namespace, nothing else."""
    kind, *rest = a.target.split("/")
    if a.type in ("change_node_sku", "set_node_pool") or kind == "nodepool":
        return ("gitops/infra/nodepools.tf",)
    if kind == "namespace":
        return ("gitops/apps/namespace.yaml",)
    return (f"gitops/apps/{rest[-1]}/",)


def _in_scope(path: str, a: Action) -> bool:
    return any(path == x or (x.endswith("/") and path.startswith(x)) for x in _scope(a))


def _files_for(actions: list[Action], clone: Path) -> dict[str, str]:
    paths = set()
    for a in actions:
        for x in _scope(a):
            if x.endswith("/") and (clone / x).is_dir():
                paths |= {p for p in (clone / x).rglob("*") if p.is_file() and p.suffix in (".yaml", ".py")}
            elif (clone / x).is_file():
                paths.add(clone / x)
        f = (a.params or {}).get("file")
        if isinstance(f, str) and _in_scope(f, a) and (clone / f).is_file():
            paths.add(clone / f)
    return {str(p.relative_to(clone)): p.read_text() for p in sorted(paths) if p.exists()}


def _bind(edits: list[Edit], actions: list[Action], by: str) -> tuple[list[Edit], list[dict]]:
    """Keep the edits that implement an approved action inside that action's files; refuse the rest."""
    approved = {a.id: a for a in actions}
    kept, refused = [], []
    for e in edits:
        a = approved.get(e.action)
        if a is None:
            refused.append({**e.model_dump(), "by": by, "reason": f"names action {e.action!r}, which the Executor was not given"})
        elif not _in_scope(e.path, a):
            refused.append({**e.model_dump(), "by": by, "reason": f"{a.id} ({a.type} {a.target}) may only change "
                                                                  f"{' or '.join(_scope(a))}"})
        else:
            kept.append(e)
    return kept, refused


def _as_approved(objs: list[dict], actions: list[Action]) -> list[str]:
    """A set_resources edit must leave the target with the requests and limits the Thinker's checks passed."""
    problems = []
    for a in actions:
        if a.type != "set_resources":
            continue
        name = a.target.split("/")[-1]
        obj = next((o for o in objs if o.get("kind") in ("Deployment", "Pod") and (o.get("metadata") or {}).get("name") == name), None)
        containers = (policy.pod_spec(obj) or {}).get("containers") or [] if obj else []
        c = next((c for c in containers if c.get("name") == name), containers[0] if containers else None)
        res = (c or {}).get("resources") or {}
        for kind in ("requests", "limits"):
            for key, want in ((a.params or {}).get(kind) or {}).items():
                got = (res.get(kind) or {}).get(key)
                try:
                    same = want is None or (got is not None and policy.qty(got) == policy.qty(want))
                except ValueError:
                    same = False
                if not same:
                    problems.append(f"{a.id} approved {kind}.{key}={want} for {name}, but the change leaves {got}")
    return problems


def _materialize(clone: Path, edits: list[Edit], skus: set[str], by: str, actions: list[Action],
                 namespaces: list[str], nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    edits, rejected = _bind(edits, actions, by)
    texts: dict[str, str | None] = {}
    originals: dict[str, str | None] = {}
    applied = []
    for e in edits:
        try:
            path = E.check_path(clone, e.path)
            if e.path not in texts:
                originals[e.path] = texts[e.path] = path.read_text() if path.exists() else None
            new = E.apply_edit(texts[e.path], e)
            E.validate_file(e.path, new, skus)
            texts[e.path] = new
            applied.append({**e.model_dump(), "by": by})
        except E.EditError as exc:
            rejected.append({**e.model_dump(), "by": by, "reason": str(exc)})
    # Check what Apply would send, per app folder (kubectl apply -k) or the namespace file (-f), and undo a unit
    # whose rendered objects leave the namespace, use another kind, carry resources the policy refuses or differ
    # from the unit as it was in any way its approved actions don't cover.
    units = sorted({"/".join(e["path"].split("/")[:3]) for e in applied if e["path"].startswith("gitops/apps/")})
    before = {}
    for unit in units:  # rendered before anything is written: what the edits change is the difference
        try:
            before[unit] = policy.render(clone / unit) if (clone / unit).exists() else []
        except (policy.PolicyError, OSError) as exc:
            before[unit] = exc
    for rel, text in texts.items():
        if text is not None:
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_text(text)
    for unit in units:
        target = clone / unit
        if target.is_dir() and not (target / "kustomization.yaml").exists():
            continue
        by_unit = [a for a in actions if any(e["action"] == a.id for e in applied if e["path"].startswith(unit))]
        try:
            if isinstance(before[unit], Exception):
                raise before[unit]
            objs = policy.render(target)
            problems = (policy.check(objs, namespaces, nodes) + _as_approved(objs, by_unit)
                        + policy.unapproved(before[unit], objs, by_unit))
        except (policy.PolicyError, OSError) as exc:
            problems = [str(exc)]
        if not problems:
            continue
        reason = "; ".join(problems)[:600]
        for e in [x for x in applied if x["path"] == unit or x["path"].startswith(unit + "/")]:
            applied.remove(e)
            rejected.append({**e, "reason": f"{unit} would break policy: {reason}"})
        for rel, text in originals.items():
            if rel == unit or rel.startswith(unit + "/"):
                if text is None:
                    (clone / rel).unlink(missing_ok=True)
                else:
                    (clone / rel).write_text(text)
    return applied, rejected


def _execute(team, s, tr, llm, diagnosis: Diagnosis, inv, catalog, run_dir: Path, charge, notify) -> None:
    ex = team.executor
    started = time.monotonic()
    tr.agent(ex.name, state="running", started=_now())
    notify()
    doable = [a for a in diagnosis.actions if not a.needs_human and a.type not in ("investigate", "remove_workload")]
    human = [Skipped(action=a.id, reason=f"needs a human: {a.summary}") for a in diagnosis.actions if a not in doable]
    base = s.base_branch or os.environ.get("KUBESAGE_AGENTS_BASE") or ex.config.settings.get("base_branch", "main")
    state = {"base": base, "branch": None, "commit": None, "edits": [], "rejected": [], "skipped": [x.model_dump() for x in human],
             "title": None, "body": None, "diff": "", "pr_url": None, "applied": None, "files": []}
    tr.set(executor=state)
    if not doable:
        _say(ex.name, "✓ nothing to change in Git" + (f"; {len(human)} action(s) left for a human" if human else ""))
        tr.agent(ex.name, state="done", summary="nothing to change", ms=int((time.monotonic() - started) * 1000))
        return
    clone = s.out_dir / "repo"
    slug = re.sub(r"[^a-z0-9-]+", "-", f"{diagnosis.issues[0].targets[0].split('/')[-1]}-{diagnosis.issues[0].category}"
                  if diagnosis.issues and diagnosis.issues[0].targets else "fix").strip("-")[:40]
    branch = f"{ex.config.settings.get('branch_prefix', 'agents/')}{tr.id}-{slug}"
    try:
        repo.ensure(clone, s.repo_url or repo.origin_url())
        base_sha = repo.start_branch(clone, base, branch)
    except Exception as exc:  # no network, no base branch, a failed clone
        _say(ex.name, f"✗ can't prepare the repo: {exc}")
        tr.agent(ex.name, state="failed", error=str(exc)[:300], ms=int((time.monotonic() - started) * 1000))
        tr.set(executor={**state, "error": str(exc)[:400]})
        return
    files = _files_for(doable, clone)
    _say(ex.name, f"▶ Executor: {len(doable)} action(s) on {len(files)} file(s) from origin/{base} ({base_sha[:8]})")
    skus = {x.name for x in catalog.skus}
    user = json.dumps({"diagnosis": {**diagnosis.model_dump(), "actions": [a.model_dump() for a in doable]},
                       "files": files, "rules": team.rules_text(),
                       "skus": [{"name": x.name, "family": x.family, "vcpu": x.vcpu, "memory_gib": x.memory_gib} for x in catalog.skus]},
                      separators=(",", ":"))
    det = {a.id: E.deterministic(a, files) for a in doable}
    try:
        cs, entry = llm.structured(ex.name, ex.prompt + contract("executor", ex), user, ChangeSet,
                                   lambda: mock.executor(diagnosis, [e for es in det.values() for e in es],
                                                         [Skipped(action=a.id, reason="the rule-based executor can't write this change; "
                                                                  "it needs the live model") for a in doable if not det[a.id]]),
                                   stage="executor", workload="team", unit="agents", flow_stage=4)
    except LLMError as exc:
        _say(ex.name, f"✗ model call failed: {exc}")
        tr.agent(ex.name, state="failed", error=str(exc)[:300])
        return
    charge(ex.name, entry)
    by = "rules" if tr.data["mode"] == "mock" else "model"
    bounds = (doable, s.namespaces, inv["nodes"])
    applied, rejected = _materialize(clone, cs.edits, skus, by, *bounds)
    done_ids = {e["action"] for e in applied}
    fallback = [e for a in doable if a.id not in done_ids and by == "model" for e in E.deterministic(a, _files_for([a], clone))]
    if fallback:
        more, more_rejected = _materialize(clone, fallback, skus, "rules-fallback", *bounds)
        applied += more
        rejected += more_rejected
    for e in applied:
        _say(ex.name, f"  ✎ {e['path']} ({e['action']}, {e['by']}): -{e['find'].count(chr(10)) + bool(e['find'])} "
                      f"+{e['replace'].count(chr(10)) + 1} lines")
    for e in rejected:
        _say(ex.name, f"  ✗ rejected {e['path']} ({e['action']}): {e['reason'][:200]}")
    skipped = [x.model_dump() for x in cs.skipped] + state["skipped"]
    body = cs.body.rstrip() + "\n\n---\n" + _footer(tr, applied, rejected, diagnosis)
    state.update(branch=branch, base_sha=base_sha, edits=applied, rejected=rejected, skipped=skipped, title=_title(cs.title),
                 body=body, files=sorted({e["path"] for e in applied}))
    if applied:
        commit = repo.commit(clone, f"{_title(cs.title)}\n\nKubeSage agent team run {tr.id}.")
        state.update(commit=commit, diff=repo.diff(clone, base_sha))
        (run_dir / "changes.diff").write_text(state["diff"])
        (run_dir / "pr_body.md").write_text(body)
        _say(ex.name, f"✓ committed {commit[:8]} on {branch}: {_title(cs.title)}")
        for line in state["diff"].splitlines()[:120]:
            _say(ex.name, f"  {line}")
    else:
        _say(ex.name, "✓ no valid edits, so nothing was committed")
    tr.set(executor=state)
    tr.agent(ex.name, state="done", findings=len(applied), summary=cs.title[:200], ms=int((time.monotonic() - started) * 1000))
    if applied and s.open_pr and ex.config.settings.get("auto_pr"):
        open_pr(tr, s, run_dir)


def _title(text: str, limit: int = 72) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def _footer(tr: TeamRun, applied: list[dict], rejected: list[dict], d: Diagnosis) -> str:
    agents = tr.data["agents"]
    lines = [f"Opened by the KubeSage **Executor** agent from team run `{tr.id}` ({tr.data['trigger']}, {tr.data['mode']} mode).", "",
             "| agent | model | tokens in / out | cost |", "|---|---|---|---|"]
    for name in tr.data["order"]:
        a = agents[name]
        lines.append(f"| {a['title']} | {a['label']} (`{a['model']}`) | {a['tokens_in']:,} / {a['tokens_out']:,} | ${a['cost_usd']:.4f} |")
    deltas = [a.params.get("monthly_delta_usd") for a in d.actions if a.type == "change_node_sku"]
    if any(x is not None for x in deltas):
        lines += ["", f"Node-pool cost change: {', '.join(f'{x:+.2f} USD/month' for x in deltas if x is not None)} "
                      "(config/skus.yaml list prices). `gitops/infra/` changes need `terraform apply`; merging doesn't resize anything."]
    if any(e["by"] != "model" for e in applied) and tr.data["mode"] == "live":
        lines += ["", "Edits marked rules-fallback were written by KubeSage's rule-based executor after the model's own "
                      "edits for that action failed validation: " + ", ".join(sorted({e['action'] for e in applied if e['by'] != 'model'}))]
    if rejected:
        lines += ["", "Rejected edits (not in this PR): " + "; ".join(f"{e['path']}: {e['reason'][:120]}" for e in rejected)]
    return "\n".join(lines) + "\n"


def open_pr(tr_or_data, s: TeamSettings, run_dir: Path) -> str:
    data = tr_or_data.data if isinstance(tr_or_data, TeamRun) else tr_or_data
    ex = data["executor"]
    if not ex or not ex.get("commit"):
        raise repo.RepoError("this run committed nothing, so there is no pull request to open")
    if ex.get("pr_url"):
        return ex["pr_url"]
    body = run_dir / "pr_body.md"
    url = repo.open_pr(s.out_dir / "repo", branch=ex["branch"], base=ex["base"], title=ex["title"], body_file=body)
    ex["pr_url"] = url
    _say("executor", f"✓ opened {url}")
    log("agents", f"executor opened {url}")
    return url


def save_run(run_dir: Path, data: dict) -> None:
    """Write run.json whole or not at all, so a crash mid-write never leaves a run that won't load."""
    tmp = run_dir / "run.json.tmp"
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, run_dir / "run.json")


def _finish(tr: TeamRun, run_dir: Path, trace: list[dict]) -> None:
    save_run(run_dir, tr.snapshot())
    (run_dir / "trace.json").write_text(json.dumps(trace, default=str))


def load_run(out_dir: Path, run_id: str) -> dict:
    if not re.fullmatch(r"\d{8}-\d{6}", run_id or ""):
        raise ValueError("invalid team run id")
    return json.loads((out_dir / run_id / "run.json").read_text())


def list_runs(out_dir: Path, limit: int = 30) -> list[dict]:
    out = []
    for p in sorted(out_dir.glob("*/run.json"), reverse=True)[:limit]:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({k: d.get(k) for k in ("id", "trigger", "state", "mode", "cost_usd", "started_at")}
                   | {"issues": len((d.get("diagnosis") or {}).get("issues", [])),
                      "pr_url": (d.get("executor") or {}).get("pr_url"), "branch": (d.get("executor") or {}).get("branch")})
    return out
