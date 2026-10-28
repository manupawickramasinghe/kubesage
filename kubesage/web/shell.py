"""The Lab's read-only shell: an allow-list of kubectl, PromQL and kubesage commands.

The Control Center has no login, so whatever this runs is open to anyone who can reach the port. Every command
here only reads: no apply/patch/delete/exec, no Secrets, no --watch or -f. There is no shell either: input is
split with shlex, each token is checked, and kubectl gets an argument list.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from kubesage import apply
from kubesage.config import load_models
from kubesage.metrics.client import PromClient, PromError

OUTPUT_LIMIT = 100_000
NAME = r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?"
RESOURCES = {  # what `get`/`describe` may read; Secrets, ServiceAccounts and RBAC are deliberately absent
    "pods": "pods", "pod": "pods", "po": "pods",
    "deployments": "deployments", "deployment": "deployments", "deploy": "deployments",
    "replicasets": "replicasets", "replicaset": "replicasets", "rs": "replicasets",
    "nodes": "nodes", "node": "nodes", "no": "nodes",
    "scaledobjects": "scaledobjects.keda.sh", "scaledobject": "scaledobjects.keda.sh", "so": "scaledobjects.keda.sh",
    "scaledobjects.keda.sh": "scaledobjects.keda.sh",
    "hpa": "hpa", "horizontalpodautoscalers": "hpa", "horizontalpodautoscaler": "hpa",
    "jobs": "jobs", "job": "jobs", "services": "services", "service": "services", "svc": "services",
    "events": "events", "event": "events", "ev": "events", "namespaces": "namespaces", "namespace": "namespaces",
    "ns": "namespaces", "endpointslices": "endpointslices",
}
VALUE_FLAGS = {  # flag -> pattern its value must match
    "-n": NAME, "--namespace": NAME, "-o": r"wide|yaml|json|name", "--output": r"wide|yaml|json|name",
    "-l": r"[-A-Za-z0-9_./=!,]{1,200}", "--selector": r"[-A-Za-z0-9_./=!,]{1,200}",
    "--sort-by": r"[.A-Za-z0-9_\[\]-]{1,100}", "-c": NAME, "--container": NAME, "--tail": r"[1-9][0-9]{0,2}",
    "--since": r"[1-9][0-9]{0,3}[smh]", "--for": rf"[a-z]+/{NAME}", "--types": r"Normal|Warning",
}
BOOL_FLAGS = {"-A", "--all-namespaces", "--show-labels", "--no-headers", "--containers", "--timestamps",
              "--all-containers"}
VERBS = {  # verb -> (flags it accepts, how many positional arguments)
    "get": ({"-n", "--namespace", "-A", "--all-namespaces", "-o", "--output", "-l", "--selector", "--show-labels",
             "--no-headers", "--sort-by"}, (1, 2)),
    "describe": ({"-n", "--namespace", "-A", "--all-namespaces", "-l", "--selector"}, (1, 2)),
    "top": ({"-n", "--namespace", "-A", "--all-namespaces", "-l", "--selector", "--containers", "--no-headers",
             "--sort-by"}, (1, 2)),
    "logs": ({"-n", "--namespace", "-c", "--container", "--tail", "--since", "--timestamps", "--all-containers"}, (1, 1)),
    "events": ({"-n", "--namespace", "-A", "--all-namespaces", "--for", "--types", "-o", "--output"}, (0, 0)),
    "version": (set(), (0, 0)), "cluster-info": (set(), (0, 0)), "api-resources": (set(), (0, 0)),
}

HELP = """KubeSage lab shell: read-only. Nothing here changes the cluster.

  kubectl get pods -o wide            kubectl get scaledobjects,hpa -n demo
  kubectl top nodes                   kubectl describe hpa keda-hpa-checkout-web
  kubectl logs deploy/checkout-web --tail 20      kubectl events -n demo --types Warning
  promql 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="demo"}[5m]))'
  promql 'container_memory_working_set_bytes{namespace="demo"}' --range 1h --step 5m
  kubesage route     model tiers and how the latest run routed each workload
  kubesage ledger    every LLM call of the latest run, with tokens, cost and latency
  kubesage plan      the node-pool decision and its notes
  kubesage runs      recent analysis runs
  kubesage config    provider, model profile and metrics endpoint
  clear · history · help

kubectl reads: get describe top logs events version cluster-info api-resources. Namespace defaults to
the server's (-n to change, -A for all). Secrets are not readable here. Changes go through the dashboard's
buttons, which keep an operation lock and an audit trail."""


class Refused(ValueError):
    pass


def run(line: str, *, namespace: str, vm: str, out_dir: Path) -> dict:
    started = time.perf_counter()
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return _result(line, 2, f"parse error: {exc}", started)
    if not tokens:
        return _result(line, 0, "", started)
    try:
        if tokens[0] == "help":
            code, out = 0, HELP
        elif tokens[0] == "kubectl":
            code, out = _kubectl(tokens[1:], namespace)
        elif tokens[0] == "promql":
            code, out = _promql(tokens[1:], vm)
        elif tokens[0] == "kubesage":
            code, out = _kubesage(tokens[1:], namespace=namespace, vm=vm, out_dir=out_dir)
        else:
            raise Refused(f"{tokens[0]}: command not available here (read-only lab). Try `help`.")
    except Refused as exc:
        code, out = 2, str(exc)
    return _result(line, code, out, started)


def _result(line: str, code: int, out: str, started: float) -> dict:
    if len(out) > OUTPUT_LIMIT:
        out = out[:OUTPUT_LIMIT] + f"\n… output cut at {OUTPUT_LIMIT:,} characters"
    return {"cmd": line, "exit": code, "out": out, "ms": int((time.perf_counter() - started) * 1000)}


def _flags(args: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    """Split `--flag=value` forms, check every flag and value; return (flag tokens, positionals)."""
    flags, positional, i = [], [], 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            name, eq, value = arg.partition("=")
            if name not in allowed:
                raise Refused(f"{name}: flag not allowed here (read-only lab)")
            if name in BOOL_FLAGS:
                if eq:
                    raise Refused(f"{name} takes no value")
                flags.append(name)
            else:
                if not eq:
                    i += 1
                    if i == len(args):
                        raise Refused(f"{name} needs a value")
                    value = args[i]
                if not re.fullmatch(VALUE_FLAGS[name], value):
                    raise Refused(f"{name} {value!r}: value not allowed")
                flags += [name, value]
        else:
            positional.append(arg)
        i += 1
    return flags, positional


def _kubectl(args: list[str], namespace: str) -> tuple[int, str]:
    if not args or args[0] not in VERBS:
        raise Refused(f"kubectl {args[0] if args else ''}: only {', '.join(VERBS)} are available here (read-only)")
    verb, rest = args[0], args[1:]
    allowed, (low, high) = VERBS[verb]
    flags, positional = _flags(rest, allowed)
    if not low <= len(positional) <= high:
        raise Refused(f"kubectl {verb}: expected {low if low == high else f'{low}-{high}'} arguments")
    if verb in ("get", "describe"):
        kinds = []
        for kind in positional[0].split(","):
            kind, _, name = kind.partition("/")
            if kind not in RESOURCES or (name and not re.fullmatch(NAME, name)):
                raise Refused(f"{kind}: not readable here. Try pods, deployments, nodes, scaledobjects, hpa, jobs, events")
            kinds.append(RESOURCES[kind] + (f"/{name}" if name else ""))
        positional[0] = ",".join(kinds)
    elif verb == "top":
        if positional[0] not in ("nodes", "node", "pods", "pod"):
            raise Refused("kubectl top: nodes or pods")
    elif verb == "logs":
        if not re.fullmatch(rf"(?:(?:pod|deploy|deployment|job)/)?{NAME}", positional[0]):
            raise Refused("kubectl logs: a pod, deploy/NAME or job/NAME")
        if "--tail" not in flags:
            flags += ["--tail", "100"]
    if len(positional) == 2 and not re.fullmatch(NAME, positional[1]):
        raise Refused(f"{positional[1]!r}: not a resource name")
    scoped = verb not in ("version", "cluster-info", "api-resources") and not {"-n", "--namespace", "-A", "--all-namespaces"} & set(flags)
    cmd = [*apply.kubectl_base(), "--request-timeout=8s", verb, *positional, *flags, *(["-n", namespace] if scoped else [])]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return 124, "kubectl timed out after 15 s"
    except OSError as exc:
        return 127, f"kubectl: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


DURATION = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]{0,4})([smhd])", value)
    if not match:
        raise Refused(f"{value!r}: use a duration like 30s, 15m, 6h or 7d")
    return int(match.group(1)) * DURATION[match.group(2)]


def _labels(metric: dict) -> str:
    name = metric.get("__name__", "")
    rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest else name or "{}"


def _promql(args: list[str], vm: str) -> tuple[int, str]:
    window = step = None
    expr = []
    i = 0
    while i < len(args):
        if args[i] in ("--range", "--step") and i + 1 < len(args):
            window, step = (_seconds(args[i + 1]), step) if args[i] == "--range" else (window, _seconds(args[i + 1]))
            i += 2
            continue
        expr.append(args[i])
        i += 1
    query = " ".join(expr).strip()
    if not query:
        raise Refused("promql: give an expression, e.g. promql 'up'")
    client = PromClient(vm, timeout=20, journal=False)
    try:
        if window:
            step = step or max(15, window // 120)
            if window > 30 * 86400 or window // step > 11000:
                raise Refused("promql: at most 30 days and 11,000 points per series")
            end = time.time()
            result = client.query_range(query, end - window, end, step)
        else:
            result = client.query(query)
    except PromError as exc:
        return 1, f"promql error: {exc}"
    lines = [f"{len(result)} series from {client.base_url}" + (f", last {args[args.index('--range') + 1]} every {step}s" if window else "")]
    for series in result[:40]:
        if window:
            values = [float(v) for _, v in series.get("values", [])]
            lines.append(f"{_labels(series['metric'])}  last={values[-1]:.6g} min={min(values):.6g} "
                         f"max={max(values):.6g} points={len(values)}" if values else _labels(series["metric"]))
        else:
            lines.append(f"{_labels(series['metric'])}  {series['value'][1]}")
    if len(result) > 40:
        lines.append(f"… {len(result) - 40} more series")
    return 0, "\n".join(lines)


def _table(headers: list[str], rows: list[list]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    widths = [max([len(h), *(len(r[i]) for r in rows)]) for i, h in enumerate(headers)]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    return "\n".join([fmt(headers), *(fmt(r) for r in rows)])


def _latest_report(out_dir: Path, namespace: str) -> tuple[str, dict]:
    for path in sorted(out_dir.glob("*/report.json"), reverse=True):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and report.get("namespace") == namespace:
            return path.parent.name, report
    raise Refused(f"no analysis run for namespace {namespace!r} yet: run one from the dashboard")


def _kubesage(args: list[str], *, namespace: str, vm: str, out_dir: Path) -> tuple[int, str]:
    sub = args[0] if args else "help"
    if args[1:]:
        raise Refused(f"kubesage {sub}: takes no arguments here")
    models = load_models()
    if sub == "config":
        import os
        p = models.provider
        return 0, _table(["setting", "value"], [
            ["provider", p.base_url], ["api key", f"${p.api_key_env} ({'set' if os.environ.get(p.api_key_env) else 'not set: mock mode'})"],
            ["profile", os.environ.get("KUBESAGE_MODELS", "default (config/models.yaml)")],
            ["metrics", vm], ["namespace", namespace], ["runs", str(out_dir)]])
    if sub == "runs":
        rows = []
        for path in sorted(out_dir.glob("*/report.json"), reverse=True)[:10]:
            try:
                r = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            pool = r.get("node_pool") or {}
            rows.append([path.parent.name, r.get("mode"), r.get("namespace"), f"${r.get('llm_cost_usd', 0):.4f}",
                         f"${pool.get('before_monthly_usd', 0) - pool.get('after_monthly_usd', 0):+.2f}/mo"])
        return 0, _table(["run", "mode", "namespace", "LLM cost", "pool change"], rows) if rows else "no runs yet"
    if sub not in ("route", "ledger", "plan"):
        return (0 if sub == "help" else 2), "kubesage route | ledger | plan | runs | config"
    run_id, report = _latest_report(out_dir, namespace)
    if sub == "route":
        tiers = _table(["tier", "label", "model", "$/1M in", "$/1M out", "extra"], [
            [name, t.label or name, t.model, t.input_per_mtok, t.output_per_mtok, json.dumps(t.extra_body) if t.extra_body else ""]
            for name, t in models.tiers.items()])
        calls = {e["workload"]: e for e in report["ledger"] if e["stage"] == "recommend"}
        rows = [[w["id"], w["triage"]["status"], w.get("tier") or "stop (no more tokens)",
                 f"{calls[w['id']]['input_tokens']}/{calls[w['id']]['output_tokens']}" if w["id"] in calls else "-",
                 f"${calls[w['id']]['cost_usd']:.4f}" if w["id"] in calls else "$0",
                 f"{calls[w['id']].get('latency_ms', 0)} ms" if w["id"] in calls else "-"] for w in report["workloads"]]
        return 0, (f"{models.provider.base_url}  ·  run {run_id} ({report['mode']})\n\n{tiers}\n\n"
                   + _table(["workload", "triage", "routed to", "tokens in/out", "cost", "latency"], rows))
    if sub == "ledger":
        rows = [[e["stage"], e["workload"], e["tier"], e["model"], e["input_tokens"], e["cached_tokens"], e["output_tokens"],
                 f"${e['cost_usd']:.4f}", f"{e.get('latency_ms', 0)} ms"] for e in report["ledger"]]
        return 0, (f"run {run_id} ({report['mode']}), total ${report['llm_cost_usd']:.4f}\n"
                   + _table(["stage", "workload", "tier", "model", "in", "cached", "out", "cost", "latency"], rows))
    pool = report.get("node_pool")
    if not pool:
        return 0, f"run {run_id}: no node-pool change proposed"
    lines = [f"run {run_id}",
             f"before  {pool['before_nodes']}x {pool['before_sku']}  ${pool['before_monthly_usd']:.2f}/mo",
             f"after   {pool['choice']['nodes']}x {pool['choice']['sku']}  ${pool['after_monthly_usd']:.2f}/mo",
             f"sized for {pool['demand_after']['cpu_cores']:.2f} cores / {pool['demand_after']['mem_mib'] / 1024:.1f} GiB",
             f"why     {pool['choice']['rationale']}", *(f"note    {n}" for n in pool.get("notes", [])),
             *(f"migrate {name}" for name in pool.get("migrations", {}))]
    return 0, "\n".join(lines)
