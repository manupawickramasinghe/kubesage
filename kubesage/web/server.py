"""`kubesage serve`: the AI Control Center web UI, a stdlib HTTP server over the same building blocks as the CLI.

POSTs must carry `X-KubeSage: 1`. Browsers can't add that header cross-origin without a CORS preflight this
server never answers, so another site open in the same browser can't trigger apply/spike/reset.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from kubesage import apply, gitops, live
from kubesage.config import load_models, load_skus
from kubesage.service import AnalysisError, analyze_now, resolve_mode

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    vm: str
    namespace: str
    days: int
    tz: str
    mode: str
    out_dir: Path
    gitops_repo: Path | None = None
    gitops_base: str | None = None
    gitops_path: str = "kubesage/demo"


def _kubectl_json(*args: str) -> dict | None:
    try:
        proc = subprocess.run([*apply.kubectl_base(), "--request-timeout=5s", *args, "-o", "json"],
                              capture_output=True, text=True, timeout=8)
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _kubectl_raw(path: str) -> dict | None:
    """A raw API GET, used for metrics.k8s.io (metrics-server). None when it isn't installed or not ready yet."""
    try:
        proc = subprocess.run([*apply.kubectl_base(), "--request-timeout=5s", "get", "--raw", path],
                              capture_output=True, text=True, timeout=8)
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _kubectl_logs(namespace: str, job: str) -> str | None:
    """Read only the bounded tail of a Job already observed in the configured namespace."""
    try:
        proc = subprocess.run([*apply.kubectl_base(), "--request-timeout=5s", "-n", namespace,
                               "logs", f"job/{job}", "--tail=100"], capture_output=True, text=True, timeout=8)
        return proc.stdout if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


_TRAFFIC_LINE = re.compile(r"t=\s*([0-9]{1,12})s ok=([0-9]{1,12}) err=([0-9]{1,12}) "
                           r"p50=([0-9]{1,12})ms p95=([0-9]{1,12})ms")


def _traffic_numbers(logs: str | None) -> dict:
    samples = [tuple(map(int, match.groups())) for line in (logs or "").splitlines()
               if (match := _TRAFFIC_LINE.fullmatch(line))]
    return {"logs_available": bool(samples),
            "succeeded_requests": sum(s[1] for s in samples) if samples else None,
            "failed_requests": sum(s[2] for s in samples) if samples else None,
            "elapsed": samples[-1][0] if samples else None,
            "p50_ms": samples[-1][3] if samples else None, "p95_ms": samples[-1][4] if samples else None}


_CPU_UNITS = {"n": 1e-6, "u": 1e-3, "m": 1.0}
_MEM_UNITS = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 ** 2,
              "k": 1e3 / 2 ** 20, "M": 1e6 / 2 ** 20, "G": 1e9 / 2 ** 20}


def cpu_m(quantity) -> float:
    """A Kubernetes CPU quantity ("250m", "2", "60429740n") in millicores."""
    q = str(quantity)
    return float(q[:-1]) * _CPU_UNITS[q[-1]] if q[-1] in _CPU_UNITS else float(q) * 1000


def mem_mi(quantity) -> float:
    """A Kubernetes memory quantity ("512Mi", "4Gi", "13824Ki", bytes) in MiB."""
    q = str(quantity)
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):  # "Mi" before "M"
        if q.endswith(suffix):
            return float(q[:-len(suffix)]) * _MEM_UNITS[suffix]
    return float(q) / 2 ** 20


def _resources(containers: list[dict], kind: str, key: str, parse) -> float | None:
    values = [parse(c["resources"][kind][key]) for c in containers if key in c.get("resources", {}).get(kind, {})]
    return round(sum(values), 1) if values else None


def _ready(obj: dict) -> bool:
    return any(c["type"] == "Ready" and c["status"] == "True" for c in obj.get("status", {}).get("conditions", []))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value, secrets: list[str]):
    """Provider errors and kubectl output can contain credentials; never send them to a browser."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _exclusive(method):
    """A reset, apply, or new analysis must not race another mutation from a second browser."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if not self._operation.acquire(blocking=False):
            return 409, {"error": f"{(self.operation or {}).get('name', 'an operation')} is already running; wait for it to finish"}
        self.operation = {"name": method.__name__, "started_at": _now()}
        try:
            return method(self, *args, **kwargs)
        finally:
            self.operation = None
            self._cluster_cached = None
            self._operation.release()
    return wrapped


def _checks(expected, actual, field="") -> list[dict]:
    """Compare the fields a strategic patch owns, including named containers and deleted limits."""
    if isinstance(expected, dict):
        live = actual if isinstance(actual, dict) else {}
        return [check for key, value in expected.items()
                for check in _checks(value, live.get(key), f"{field}.{key}".lstrip("."))]
    if isinstance(expected, list) and expected and all(isinstance(v, dict) and "name" in v for v in expected):
        live = {v.get("name"): v for v in (actual or []) if isinstance(v, dict)}
        return [check for value in expected
                for check in _checks({k: v for k, v in value.items() if k != "name"}, live.get(value["name"]),
                                     f"{field}[{value['name']}]")]
    # The API canonicalises resource quantities (1000m -> 1, 1024Mi -> 1Gi).
    if expected is not None and actual is not None and ".resources." in field:
        try:
            parse = cpu_m if field.endswith(".cpu") else mem_mi
            match = math.isclose(parse(expected), parse(actual))
        except (ValueError, TypeError, IndexError):
            match = expected == actual
    else:
        match = expected == actual
    return [{"field": field, "expected": expected, "actual": actual, "match": match}]


class Api:
    def __init__(self, settings: Settings):
        self.s = settings
        self._operation = threading.Lock()
        self.operation = None
        self._cluster_lock = threading.Lock()
        self._cluster_cached = None
        self._traffic_cached = None

    def config(self) -> tuple[int, dict]:
        models = load_models()
        return 200, {"namespace": self.s.namespace, "mode": resolve_mode(self.s.mode, models),
                     "tiers": {name: tier.model for name, tier in models.tiers.items()},
                     "gitops": self.s.gitops_repo is not None and self.s.gitops_base is not None,
                     "operation": self.operation}

    def _run(self, name: str | None = None, *, mutation: bool = False) -> Path:
        if name is None:
            if mutation:
                name = apply.latest_run(self.s.out_dir).name
            else:
                for path in sorted(self.s.out_dir.glob("*/report.json"), reverse=True):
                    try:
                        return self._run(path.parent.name)
                    except (FileNotFoundError, ValueError):
                        continue
                raise FileNotFoundError(f"no analysis yet for namespace {self.s.namespace!r}")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise ValueError("invalid analysis run")
        run = (self.s.out_dir / name).resolve()
        if (not run.is_relative_to(self.s.out_dir.resolve()) or not (run / "manifests").is_dir()
                or not (run / "report.json").is_file()):
            raise FileNotFoundError("analysis run not found")
        report = json.loads((run / "report.json").read_text())
        if not isinstance(report, dict):
            raise ValueError("invalid analysis report")
        if report.get("namespace") != self.s.namespace:
            raise ValueError(f"Analysis run belongs to namespace {report.get('namespace')!r}; "
                             f"this server is configured for {self.s.namespace!r}.")
        return run

    def report(self, name: str | None = None) -> tuple[int, dict]:
        try:
            run = self._run(name)
            report = json.loads((run / "report.json").read_text())
        except FileNotFoundError:
            return 404, {"error": "no analysis yet"}
        except ValueError:
            return 400, {"error": "invalid analysis run or report"}
        return 200, {"run": run.name, "report": report}

    def reports(self) -> tuple[int, dict]:
        runs = []
        for path in sorted(self.s.out_dir.glob("*/report.json"), reverse=True):
            if path.parent.name.startswith(".") or not (path.parent / "manifests").is_dir():
                continue
            try:
                report = json.loads(path.read_text())
                if not isinstance(report, dict) or report.get("namespace") != self.s.namespace:
                    continue
                pool = report.get("node_pool") or {}
                runs.append({"run": path.parent.name, "generated_at": report.get("generated_at"),
                             "mode": report.get("mode"), "namespace": report.get("namespace"),
                             "llm_cost_usd": report.get("llm_cost_usd"),
                             "monthly_savings_usd": round(pool.get("before_monthly_usd", 0)
                                                          - pool.get("after_monthly_usd", 0), 2)})
            except (OSError, ValueError, TypeError):
                continue  # an interrupted write must not hide the other completed runs
            if len(runs) == 50:
                break
        return 200, {"runs": runs}

    @_exclusive
    def analyze(self) -> tuple[int, dict]:
        try:
            result, run_dir, _ = analyze_now(vm=self.s.vm, namespace=self.s.namespace, days=self.s.days, tz=self.s.tz,
                                             mode=self.s.mode, out_dir=self.s.out_dir)
        except AnalysisError as exc:
            return 502, {"error": str(exc)}
        return 200, {"run": run_dir.name, "report": json.loads(result.model_dump_json())}

    @_exclusive
    def apply(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body.get("dry_run", False), bool):
            return 400, {"error": "dry_run must be true or false"}
        try:
            run = self._run(body.get("run"), mutation=True)
        except FileNotFoundError as exc:
            return 404, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        steps = apply.run(apply.plan(run, dry_run=bool(body.get("dry_run")),
                                     available_node_counts=apply.ready_node_counts(load_skus().node_label)))
        ok = all(s.returncode == 0 or s.skipped for s in steps)
        return (200 if ok else 502), {"run": run.name, "dry_run": bool(body.get("dry_run")), "ok": ok,
                                    "error": None if ok else "Some changes failed; review the step output before retrying.", "steps": [
            {"file": s.file.name, "ok": s.returncode == 0, "skipped": s.skipped, "output": s.output} for s in steps]}

    @_exclusive
    def spike(self, body: dict) -> tuple[int, dict]:
        target = body.get("target", "checkout-web")
        if not isinstance(target, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", target):
            return 400, {"error": "target must be a Service name in this namespace"}
        values = {}
        for key, lo, hi, default in (("rps", 1, 200, 40.0), ("work_ms", 1, 500, 50), ("seconds", 10, 900, 180)):
            value = body.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
                return 400, {"error": f"{key} must be a number from {lo} to {hi}"}
            if key != "rps" and value != int(value):
                return 400, {"error": f"{key} must be a whole number"}
            values[key] = type(default)(value)
        job = live.spike_job(namespace=self.s.namespace, target=target, **values)
        try:
            proc = live.start_spike(job)
        except subprocess.TimeoutExpired:
            return 502, {"error": "Starting traffic timed out after 30 seconds; check the cluster before retrying."}
        except OSError as exc:
            return 502, {"error": f"Could not start traffic: {exc}"}
        if proc.returncode:
            return 502, {"error": proc.stderr.strip()}
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        return 200, {"job": job["metadata"]["name"], **env}

    @_exclusive
    def reset(self) -> tuple[int, dict]:
        if self.s.namespace != "demo":
            return 400, {"error": "Reset demo is only available for the demo namespace"}
        try:
            proc = subprocess.run(["bash", str(REPO_ROOT / "scripts/demo-reset.sh")], capture_output=True, text=True,
                                  timeout=300)
        except subprocess.TimeoutExpired:
            return 502, {"ok": False, "error": "Reset timed out after 300 seconds; inspect the cluster before retrying.",
                         "output": []}
        except OSError as exc:
            return 502, {"ok": False, "error": f"Could not run demo reset: {exc}", "output": []}
        output = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
        ok = proc.returncode == 0
        return (200 if ok else 502), {"ok": ok, "output": output,
                                    "error": None if ok else f"Reset failed: {output[-1] if output else f'exit code {proc.returncode}'}"}

    @_exclusive
    def gitops(self, body: dict) -> tuple[int, dict]:
        if self.s.gitops_repo is None or self.s.gitops_base is None:
            return 400, {"error": "start the server with --gitops-repo and --gitops-base"}
        if not isinstance(body.get("open_pr", False), bool):
            return 400, {"error": "open_pr must be true or false"}
        try:
            out = gitops.export(self._run(body.get("run"), mutation=True), self.s.gitops_repo, path=self.s.gitops_path,
                                kustomize_base=self.s.gitops_base, open_pr=bool(body.get("open_pr")))
        except (FileNotFoundError, gitops.GitOpsError) as exc:
            return 502, {"error": str(exc)}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 200, {"branch": out.branch, "commit": out.commit, "pushed": out.pushed, "pr_url": out.pr_url}

    def _plan_status(self, objects: list[dict], nodes: list[dict]) -> dict:
        try:
            run = self._run()
        except FileNotFoundError:
            return {"run": None, "state": "none", "matched": 0, "total": 0, "workloads": {}}
        actual = {(o.get("kind"), o.get("metadata", {}).get("name")): o for o in objects}
        workloads = {}
        try:
            report = json.loads((run / "report.json").read_text())
            for path in sorted((run / "manifests").glob("*.yaml")):
                desired = yaml.safe_load(path.read_text())
                kind, name = desired["kind"], desired["metadata"]["name"]
                if kind not in {"Deployment", "ScaledObject"}:
                    continue
                if desired["metadata"].get("namespace", self.s.namespace) != self.s.namespace:
                    continue
                app = desired["spec"].get("scaleTargetRef", {}).get("name", name)
                row = workloads.setdefault(app, {"checks": []})
                obj = actual.get((kind, name))
                checks = _checks(desired["spec"], (obj or {}).get("spec"), kind)
                if obj is None:
                    for check in checks:
                        check["match"] = False
                row["checks"].extend(checks)
            for row in workloads.values():
                checks = row["checks"]
                matched = sum(c["match"] for c in checks)
                row["state"] = "applied" if checks and matched == len(checks) else "partial" if matched else "pending"
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            return {"run": run.name, "state": "unknown", "matched": 0, "total": 0, "workloads": {}}
        matched = sum(w["state"] == "applied" for w in workloads.values())
        total = len(workloads)
        state = "none" if not total else "applied" if matched == total else \
            "partial" if any(w["state"] != "pending" for w in workloads.values()) else "pending"
        ready = all((o.get("status", {}).get("readyReplicas", 0) >= o.get("spec", {}).get("replicas", 1)
                     and o.get("status", {}).get("updatedReplicas", 0) >= o.get("spec", {}).get("replicas", 1)
                     and o.get("status", {}).get("observedGeneration", 0) >= o.get("metadata", {}).get("generation", 0))
                    for (kind, name), o in actual.items() if kind == "Deployment" and name in workloads)
        pool = report.get("node_pool") or {}
        choice = pool.get("choice") or {}
        sku, required = choice.get("sku"), choice.get("nodes")
        ready_nodes = sum(_ready(n) and not n.get("spec", {}).get("unschedulable", False)
                          and n.get("metadata", {}).get("labels", {}).get(pool.get("node_label", "kubesage.io/sku")) == sku
                          for n in nodes)
        capacity = {"sku": sku, "required": required, "ready": ready_nodes,
                    "available": required is None or ready_nodes >= required}
        if state == "applied" and not capacity["available"]:
            state = "partial"
        return {"run": run.name, "state": state, "matched": matched, "total": total,
                "ready": ready and state == "applied", "workloads": workloads, "capacity": capacity}

    def _traffic(self, objects: list[dict]) -> dict | None:
        jobs = [o for o in objects if o.get("kind") == "Job"
                and o.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/managed-by") == "kubesage"
                and re.fullmatch(r"kubesage-spike-[a-z0-9][a-z0-9-]*", o.get("metadata", {}).get("name", ""))
                and o["metadata"].get("namespace", self.s.namespace) == self.s.namespace]
        if not jobs:
            return None
        job = max(jobs, key=lambda o: (o["metadata"].get("creationTimestamp", ""), o["metadata"]["name"]))
        meta, status = job["metadata"], job.get("status", {})
        conditions = {c.get("type") for c in status.get("conditions", []) if c.get("status") == "True"}
        state = "failed" if status.get("failed") or "Failed" in conditions else \
            "completed" if status.get("succeeded") or "Complete" in conditions else "running" if status.get("active") else "pending"
        key = (meta["name"], meta.get("uid"), state)
        if not self._traffic_cached or self._traffic_cached[0] != key or time.monotonic() - self._traffic_cached[1] >= 10:
            numbers = _traffic_numbers(_kubectl_logs(self.s.namespace, meta["name"]))
            self._traffic_cached = (key, time.monotonic(), numbers)
        return {"job": meta["name"], "status": state, **self._traffic_cached[2]}

    def cluster(self) -> tuple[int, dict]:
        # Several tabs share one short-lived sample; a slow Kubernetes API must not fan out unbounded calls.
        with self._cluster_lock:
            if self._cluster_cached and time.monotonic() - self._cluster_cached[0] < 2:
                return 200, {**self._cluster_cached[1], "operation": self.operation}
            result = self._read_cluster()
            self._cluster_cached = (time.monotonic(), result[1])
            return result

    def _read_cluster(self) -> tuple[int, dict]:
        nodes = _kubectl_json("get", "nodes")
        if nodes is None:
            return 200, {"available": False}
        ns = self.s.namespace
        def get_objects():
            return _kubectl_json("-n", ns, "get", "pods,deployments,scaledobjects.keda.sh,hpa,jobs") \
                or _kubectl_json("-n", ns, "get", "pods,deployments,hpa,jobs") \
                or _kubectl_json("-n", ns, "get", "pods,deployments")

        with ThreadPoolExecutor(max_workers=3) as pool:
            objects_future = pool.submit(get_objects)
            pod_future = pool.submit(_kubectl_raw, f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods")
            node_future = pool.submit(_kubectl_raw, "/apis/metrics.k8s.io/v1beta1/nodes")
            objs, pod_metrics, node_metrics = objects_future.result(), pod_future.result(), node_future.result()
        objects_available = objs is not None
        objs = objs or {"items": []}
        usage = {}
        for m in (pod_metrics or {}).get("items", []):
            cs = m.get("containers", [])
            usage[m["metadata"]["name"]] = (round(sum(cpu_m(c["usage"]["cpu"]) for c in cs), 1),
                                            round(sum(mem_mi(c["usage"]["memory"]) for c in cs), 1))
        node_usage = {m["metadata"]["name"]: (round(cpu_m(m["usage"]["cpu"])), round(mem_mi(m["usage"]["memory"])))
                      for m in (node_metrics or {}).get("items", [])}
        catalog = load_skus()
        prices = {s.name: round(s.hourly_usd * catalog.hours_per_month, 2) for s in catalog.skus}
        by_node = {}
        for n in nodes["items"]:
            meta, status = n["metadata"], n["status"]
            labels, notes, info = meta.get("labels", {}), meta.get("annotations", {}), status.get("nodeInfo", {})
            addresses = {a["type"]: a["address"] for a in status.get("addresses", [])}
            capacity, allocatable = status.get("capacity", {}), status.get("allocatable", {})
            used = node_usage.get(meta["name"])
            pool = labels.get(catalog.node_label)
            by_node[meta["name"]] = {
                "name": meta["name"], "ready": _ready(n), "created": meta.get("creationTimestamp"),
                "role": "control-plane" if "node-role.kubernetes.io/control-plane" in labels else "worker",
                # The real machine: kubesage.io/* annotations are set by cluster-up.sh and join-node.sh.
                "host": notes.get("kubesage.io/host") or labels.get("kubernetes.io/hostname", meta["name"]),
                "cpu_model": notes.get("kubesage.io/cpu"), "os": notes.get("kubesage.io/os") or info.get("osImage"),
                "kernel": info.get("kernelVersion"), "kubelet": info.get("kubeletVersion"),
                "runtime": info.get("containerRuntimeVersion"), "k3d": meta["name"].startswith("k3d-"),
                "internal_ip": addresses.get("InternalIP"), "external_ip": addresses.get("ExternalIP"),
                "cpu_cores": cpu_m(capacity["cpu"]) / 1000 if "cpu" in capacity else None,
                "mem_mi": round(mem_mi(capacity["memory"])) if "memory" in capacity else None,
                "alloc_cpu_m": round(cpu_m(allocatable["cpu"])) if "cpu" in allocatable else None,
                "alloc_mem_mi": round(mem_mi(allocatable["memory"])) if "memory" in allocatable else None,
                "cpu_used_m": used[0] if used else None, "mem_used_mi": used[1] if used else None,
                # The cost model, not the hardware: what this node's pool label costs in config/skus.yaml.
                "pool": pool, "pool_monthly_usd": prices.get(pool),
                "pods": []}
        unscheduled = {"name": "(pending)", "host": "unscheduled", "pool": None, "ready": False, "role": "pending", "pods": []}
        scaling, deployments, active_jobs = {}, {}, []
        for o in objs["items"]:
            kind, meta = o["kind"], o["metadata"]
            if kind == "Pod":
                containers, used = o["spec"].get("containers", []), usage.get(meta["name"])
                pod = {"name": meta["name"], "app": meta.get("labels", {}).get("app", meta["name"]),
                       "phase": "Terminating" if meta.get("deletionTimestamp") else o["status"].get("phase"),
                       "ready": _ready(o),
                       "cpu_request_m": _resources(containers, "requests", "cpu", cpu_m),
                       "mem_request_mi": _resources(containers, "requests", "memory", mem_mi),
                       "mem_limit_mi": _resources(containers, "limits", "memory", mem_mi),
                       "cpu_used_m": used[0] if used else None, "mem_used_mi": used[1] if used else None,
                       "restarts": sum(c.get("restartCount", 0) for c in o["status"].get("containerStatuses") or [])}
                by_node.get(o["spec"].get("nodeName"), unscheduled)["pods"].append(pod)
            elif kind == "ScaledObject":
                target = o["spec"]["scaleTargetRef"]["name"]
                scaling.setdefault(target, {})["min"] = o["spec"].get("minReplicaCount")
                scaling[target]["max"] = o["spec"].get("maxReplicaCount")
                scaling[target]["triggers"] = [t["type"] for t in o["spec"]["triggers"]]
                cpu_target = next((t.get("metadata", {}).get("value") for t in o["spec"]["triggers"]
                                   if t["type"] == "cpu"), None)
                if cpu_target is not None:
                    scaling[target]["cpu_target"] = float(cpu_target)
            elif kind == "HorizontalPodAutoscaler":
                target = o["spec"]["scaleTargetRef"]["name"]
                cpu = next((m["resource"]["current"].get("averageUtilization") for m in o["status"].get("currentMetrics") or []
                            if m.get("type") == "Resource" and m["resource"]["name"] == "cpu"), None)
                scaling.setdefault(target, {}).update(current=o["status"].get("currentReplicas"),
                                                      desired=o["status"].get("desiredReplicas"), cpu=cpu)
                cpu_target = next((m["resource"].get("target", {}).get("averageUtilization")
                                   for m in o["spec"].get("metrics", [])
                                   if m.get("type") == "Resource" and m["resource"]["name"] == "cpu"), None)
                if cpu_target is not None:
                    scaling[target]["cpu_target"] = cpu_target
                scaling[target].setdefault("min", o["spec"].get("minReplicas", 1))
                scaling[target].setdefault("max", o["spec"].get("maxReplicas"))
                scaling[target]["conditions"] = [{"type": c["type"], "status": c["status"],
                                                    "reason": c.get("reason"), "message": c.get("message")}
                                                   for c in o["status"].get("conditions", [])]
            elif kind == "Deployment":
                template = o["spec"].get("template", {}).get("spec", {})
                containers = template.get("containers", [])
                status = o.get("status", {})
                deployments[meta["name"]] = {
                    "replicas": o["spec"].get("replicas", 1), "ready": status.get("readyReplicas", 0),
                    "updated": status.get("updatedReplicas", 0), "available": status.get("availableReplicas", 0),
                    "node_selector": template.get("nodeSelector", {}),
                    "cpu_request_m": _resources(containers, "requests", "cpu", cpu_m),
                    "mem_request_mi": _resources(containers, "requests", "memory", mem_mi),
                    "cpu_limit_m": _resources(containers, "limits", "cpu", cpu_m),
                    "mem_limit_mi": _resources(containers, "limits", "memory", mem_mi)}
            elif kind == "Job" and meta["name"].startswith("kubesage-spike-"):
                status = o.get("status", {})
                if status.get("succeeded") or status.get("failed"):
                    continue
                containers = o.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                env = {e["name"]: e.get("value") for c in containers for e in c.get("env", [])}
                started = status.get("startTime")
                seconds = int(env.get("SECONDS") or 0)
                ends = (datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() + seconds) if started else None
                active_jobs.append({"name": meta["name"], "started_at": started, "seconds": seconds,
                                    "ends_at": datetime.fromtimestamp(ends, timezone.utc).isoformat() if ends else None,
                                    "target": env.get("TARGET"), "rps": env.get("RPS"), "work_ms": env.get("WORK_MS"),
                                    "status": "running" if status.get("active") else "pending"})
        node_list = list(by_node.values()) + ([unscheduled] if unscheduled["pods"] else [])
        plan = self._plan_status(objs["items"], nodes["items"])
        if not objects_available:
            plan.update(state="unknown", ready=False)
        return 200, {"available": True, "namespace": ns, "metrics": bool(usage), "nodes": node_list,
                     "scaling": scaling, "deployments": deployments, "plan": plan, "active_jobs": active_jobs,
                     "traffic": self._traffic(objs["items"]), "observed_at": _now(), "operation": self.operation}


def make_handler(api: Api):
    static = resources.files("kubesage.web").joinpath("static")
    page = static.joinpath("index.html").read_bytes()
    # Vendored rather than loaded from a CDN, so the page still renders on bad venue Wi-Fi.
    fonts = {f"/fonts/{f.name}": f.read_bytes() for f in static.joinpath("fonts").iterdir() if f.name.endswith(".woff2")}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict | bytes, content_type: str = "application/json",
                  cache: str = "no-store") -> None:
            secrets = [value for key, value in os.environ.items() if len(value) >= 4
                       and any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "PASSWORD", "SECRET"))]
            body = payload if isinstance(payload, bytes) else json.dumps(_redact(payload, secrets)).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path)
            routes = {"/api/config": api.config, "/api/report": lambda: api.report(parse_qs(path.query).get("run", [None])[0]),
                      "/api/reports": api.reports, "/api/cluster": api.cluster}
            if path.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path.path in fonts:
                self._send(200, fonts[path.path], "font/woff2", cache="public, max-age=86400")
            elif path.path in routes:
                try:
                    self._send(*routes[path.path]())
                except Exception as exc:
                    self._send(502, {"error": f"{type(exc).__name__}: {exc}"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.headers.get("X-KubeSage") != "1":
                self._send(403, {"error": "missing X-KubeSage header"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"error": "invalid Content-Length"})
                self.close_connection = True
                return
            if not 0 <= length <= 65536:
                self._send(413, {"error": "request body must be at most 64 KiB"})
                self.close_connection = True
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "invalid JSON"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "JSON body must be an object"})
                return
            routes = {"/api/analyze": lambda: api.analyze(), "/api/apply": lambda: api.apply(body),
                      "/api/spike": lambda: api.spike(body), "/api/reset": lambda: api.reset(),
                      "/api/gitops": lambda: api.gitops(body)}
            if self.path not in routes:
                self._send(404, {"error": "not found"})
                return
            try:
                self._send(*routes[self.path]())
            except Exception as exc:  # surface failures in the UI's activity log instead of a dropped connection
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, *args):
            pass

    return Handler


def serve(settings: Settings, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(Api(settings)))
