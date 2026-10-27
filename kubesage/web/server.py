"""`kubesage serve`: the AI Control Center web UI, a stdlib HTTP server over the same building blocks as the CLI.

POSTs must carry `X-KubeSage: 1`. Browsers can't add that header cross-origin without a CORS preflight this
server never answers, so another site open in the same browser can't trigger apply/spike/reset.
"""
from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

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
    proc = subprocess.run([*apply.kubectl_base(), *args, "-o", "json"], capture_output=True, text=True, timeout=20)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def _kubectl_raw(path: str) -> dict | None:
    """A raw API GET, used for metrics.k8s.io (metrics-server). None when it isn't installed or not ready yet."""
    proc = subprocess.run([*apply.kubectl_base(), "get", "--raw", path], capture_output=True, text=True, timeout=20)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


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
    return any(c["type"] == "Ready" and c["status"] == "True" for c in obj["status"].get("conditions", []))


def _clamp(value, lo, hi, default):
    try:
        return min(max(type(default)(value), lo), hi)
    except (TypeError, ValueError):
        return default


class Api:
    def __init__(self, settings: Settings):
        self.s = settings
        self._analysis = threading.Lock()

    def config(self) -> tuple[int, dict]:
        models = load_models()
        return 200, {"namespace": self.s.namespace, "mode": resolve_mode(self.s.mode, models),
                     "tiers": {name: tier.model for name, tier in models.tiers.items()},
                     "gitops": self.s.gitops_repo is not None}

    def report(self) -> tuple[int, dict]:
        try:
            run = apply.latest_run(self.s.out_dir)
        except FileNotFoundError:
            return 404, {"error": "no analysis yet"}
        return 200, {"run": run.name, "report": json.loads((run / "report.json").read_text())}

    def analyze(self) -> tuple[int, dict]:
        if not self._analysis.acquire(blocking=False):
            return 409, {"error": "an analysis is already running"}
        try:
            result, run_dir, _ = analyze_now(vm=self.s.vm, namespace=self.s.namespace, days=self.s.days, tz=self.s.tz,
                                             mode=self.s.mode, out_dir=self.s.out_dir)
        except AnalysisError as exc:
            return 502, {"error": str(exc)}
        finally:
            self._analysis.release()
        return 200, {"run": run_dir.name, "report": json.loads(result.model_dump_json())}

    def apply(self, body: dict) -> tuple[int, dict]:
        try:
            run = apply.latest_run(self.s.out_dir)
        except FileNotFoundError as exc:
            return 404, {"error": str(exc)}
        steps = apply.run(apply.plan(run, dry_run=bool(body.get("dry_run")),
                                     available_skus=apply.node_skus(load_skus().node_label)))
        return 200, {"run": run.name, "dry_run": bool(body.get("dry_run")), "steps": [
            {"file": s.file.name, "ok": s.returncode == 0, "skipped": s.skipped, "output": s.output} for s in steps]}

    def spike(self, body: dict) -> tuple[int, dict]:
        job = live.spike_job(namespace=self.s.namespace, target=str(body.get("target") or "checkout-web"),
                             rps=_clamp(body.get("rps"), 1.0, 200.0, 40.0), work_ms=_clamp(body.get("work_ms"), 1, 500, 50),
                             seconds=_clamp(body.get("seconds"), 10, 900, 180))
        proc = live.start_spike(job)
        if proc.returncode:
            return 502, {"error": proc.stderr.strip()}
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        return 200, {"job": job["metadata"]["name"], **env}

    def reset(self) -> tuple[int, dict]:
        proc = subprocess.run(["bash", str(REPO_ROOT / "scripts/demo-reset.sh")], capture_output=True, text=True,
                              timeout=300)
        output = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
        return (200 if proc.returncode == 0 else 502), {"ok": proc.returncode == 0, "output": output}

    def gitops(self, body: dict) -> tuple[int, dict]:
        if self.s.gitops_repo is None or self.s.gitops_base is None:
            return 400, {"error": "start the server with --gitops-repo and --gitops-base"}
        try:
            out = gitops.export(apply.latest_run(self.s.out_dir), self.s.gitops_repo, path=self.s.gitops_path,
                                kustomize_base=self.s.gitops_base, open_pr=bool(body.get("open_pr")))
        except (FileNotFoundError, gitops.GitOpsError) as exc:
            return 502, {"error": str(exc)}
        return 200, {"branch": out.branch, "commit": out.commit, "pushed": out.pushed, "pr_url": out.pr_url}

    def cluster(self) -> tuple[int, dict]:
        nodes = _kubectl_json("get", "nodes")
        if nodes is None:
            return 200, {"available": False}
        ns = self.s.namespace
        objs = _kubectl_json("-n", ns, "get", "pods,scaledobjects.keda.sh,hpa") \
            or _kubectl_json("-n", ns, "get", "pods") or {"items": []}
        usage = {}
        for m in (_kubectl_raw(f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods") or {}).get("items", []):
            cs = m.get("containers", [])
            usage[m["metadata"]["name"]] = (round(sum(cpu_m(c["usage"]["cpu"]) for c in cs), 1),
                                            round(sum(mem_mi(c["usage"]["memory"]) for c in cs), 1))
        catalog = load_skus()
        skus = {s.name: s for s in catalog.skus}
        by_node = {}
        for n in nodes["items"]:
            labels = n["metadata"].get("labels", {})
            sku = skus.get(labels.get(catalog.node_label))
            by_node[n["metadata"]["name"]] = {
                "name": n["metadata"]["name"], "sku": labels.get(catalog.node_label), "ready": _ready(n),
                "role": "control-plane" if "node-role.kubernetes.io/control-plane" in labels else "worker",
                # Sizes come from the SKU the node is labelled as, not the k3d container's real (host-sized) capacity.
                **({"family": sku.family, "vcpu": sku.vcpu, "memory_gib": sku.memory_gib,
                    "allocatable_cpu_m": round(sku.vcpu * 1000 * catalog.allocatable_cpu),
                    "allocatable_mem_mi": round(sku.memory_gib * 1024 * catalog.allocatable_mem),
                    "monthly_usd": round(sku.hourly_usd * catalog.hours_per_month, 2)} if sku else {}),
                "pods": []}
        unscheduled = {"name": "(pending)", "sku": None, "ready": False, "role": "pending", "pods": []}
        scaling = {}
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
            elif kind == "HorizontalPodAutoscaler":
                target = o["spec"]["scaleTargetRef"]["name"]
                cpu = next((m["resource"]["current"].get("averageUtilization") for m in o["status"].get("currentMetrics") or []
                            if m.get("type") == "Resource" and m["resource"]["name"] == "cpu"), None)
                scaling.setdefault(target, {}).update(current=o["status"].get("currentReplicas"),
                                                      desired=o["status"].get("desiredReplicas"), cpu=cpu)
        node_list = list(by_node.values()) + ([unscheduled] if unscheduled["pods"] else [])
        return 200, {"available": True, "namespace": ns, "metrics": bool(usage), "nodes": node_list,
                     "scaling": scaling}


def make_handler(api: Api):
    static = resources.files("kubesage.web").joinpath("static")
    page = static.joinpath("index.html").read_bytes()
    # Vendored rather than loaded from a CDN, so the page still renders on bad venue Wi-Fi.
    fonts = {f"/fonts/{f.name}": f.read_bytes() for f in static.joinpath("fonts").iterdir() if f.name.endswith(".woff2")}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict | bytes, content_type: str = "application/json",
                  cache: str = "no-store") -> None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            routes = {"/api/config": api.config, "/api/report": api.report, "/api/cluster": api.cluster}
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path in fonts:
                self._send(200, fonts[self.path], "font/woff2", cache="public, max-age=86400")
            elif self.path in routes:
                self._send(*routes[self.path]())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.headers.get("X-KubeSage") != "1":
                self._send(403, {"error": "missing X-KubeSage header"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid JSON"})
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
