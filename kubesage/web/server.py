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
        objs = _kubectl_json("-n", self.s.namespace, "get", "pods,scaledobjects.keda.sh,hpa") \
            or _kubectl_json("-n", self.s.namespace, "get", "pods") or {"items": []}
        label = load_skus().node_label
        by_node = {n["metadata"]["name"]: {"name": n["metadata"]["name"], "sku": n["metadata"].get("labels", {}).get(label),
                                           "pods": []} for n in nodes["items"]}
        unscheduled = {"name": "(pending)", "sku": None, "pods": []}
        scaling = {}
        for o in objs["items"]:
            kind, meta = o["kind"], o["metadata"]
            if kind == "Pod":
                ready = any(c["type"] == "Ready" and c["status"] == "True" for c in o["status"].get("conditions", []))
                pod = {"name": meta["name"], "app": meta.get("labels", {}).get("app", meta["name"]),
                       "phase": "Terminating" if meta.get("deletionTimestamp") else o["status"].get("phase"),
                       "ready": ready}
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
        return 200, {"available": True, "nodes": node_list, "scaling": scaling}


def make_handler(api: Api):
    page = resources.files("kubesage.web").joinpath("static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict | bytes, content_type: str = "application/json") -> None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            routes = {"/api/config": api.config, "/api/report": api.report, "/api/cluster": api.cluster}
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
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
