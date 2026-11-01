"""Replay server for the KubeSage demo video.

Serves the real dashboard/ (and the video's slides under /demo/) and answers the dashboard's API from data recorded on
the lab cluster on 29 October 2026: the latest FinOps analysis and its trace, the cluster as it was, team run
20261029-034845 and the ring lines it wrote, shown on the hypothetical cluster of scenario.py. The state moves on as the
page presses its buttons (Analyze, Apply, Send traffic, Start test, Dry-run apply). Nothing here talks to a cluster or
a model. Traffic replays the recorded 60 req/s run of 2026-10-27 (docs/testing.md) twenty times faster.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import scenario

DATA = Path(__file__).parent / "data"
REPO = Path(__file__).resolve().parents[2]
DASHBOARD, DEMO = REPO / "dashboard", REPO / "demo"
TRAFFIC_S = 9           # the recorded 180 s run, shown in 9 s
SPEED = 180 / TRAFFIC_S
ANALYZE_S = 3           # a live analysis takes a few seconds: statistics, one triage call, then more reasoning
ALERT_S = 0.5           # on the cluster the alert takes about a minute
TEAM_WAIT_S = 0.3       # and the team then waits 45 s for related alerts
APPLY_SPEED = 2         # Apply's rollout, twice as fast as the recorded one


def load(name):
    return scenario.apply(json.loads((DATA / name).read_text()))


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def epoch(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


class Sim:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = load("config.json")
        self.report = load("report.json")
        self.reports = load("reports.json")
        self.trace = load("trace.json")
        self.cluster0 = load("cluster.json")
        agents = load("agents.json")
        self.team_info, self.final = agents["team"], agents["run"]
        self.t0 = epoch(self.final["started_at"])
        self.tcs = load("testcases.json")
        self.run_lines = [x for x in load("run_lines.json") if scenario.keep(x)]
        tc = load("ring_testcase_memory.json")["records"]
        self.ledger_lines = [x["text"] for x in tc if "ledger resident" in x["text"]][:5]
        self.alert_text = next(x["text"] for x in load("ring_alerts.json")["records"] if "ContainerOOMKilled" in x["text"])
        self.analyzed = False
        self.apply_at = self.traffic_at = self.tc_at = self.alert_at = self.run_at = None
        self.pending_until = None
        self.auto = True
        self.job = "kubesage-spike-000"
        self.ring: list[dict] = []
        self.started = time.time()
        self._after_spec()

    # ---- the cluster ----
    def _after_spec(self):
        before = [p for n in self.cluster0["nodes"] for p in n["pods"]]
        recs = {w["id"].split("/")[-1]: w.get("recommendation") or {} for w in self.report["report"]["workloads"]}
        self.after = {}
        for app in sorted({p["app"] for p in before}):
            pods = [p for p in before if p["app"] == app]
            res, sc = recs.get(app, {}).get("resources") or {}, recs.get(app, {}).get("scaling") or {}
            replicas = sc.get("min_replicas", len(pods)) if sc else len(pods)
            self.after[app] = {
                "replicas": replicas,
                "cpu_request_m": res.get("cpu_request_m") or pods[0]["cpu_request_m"],
                "mem_request_mi": res.get("mem_request_mi") or pods[0]["mem_request_mi"],
                "mem_limit_mi": res.get("mem_limit_mi") or pods[0]["mem_limit_mi"],
                "cpu_used_m": sum(p["cpu_used_m"] or 0 for p in pods) / len(pods),
                "mem_used_mi": sum(p["mem_used_mi"] or 0 for p in pods) / len(pods),
                "scaling": sc}

    def _pod(self, app, i, phase, used=True, cpu=None):
        a = self.after[app]
        return {"name": f"{app}-7d9f8c6b5-{'abcdefghjk'[i % 10]}{'xq2p4m7r9t'[i % 10]}{i}", "app": app, "phase": phase,
                "ready": phase == "Running", "cpu_request_m": float(a["cpu_request_m"]), "mem_request_mi": float(a["mem_request_mi"]),
                "mem_limit_mi": float(a["mem_limit_mi"]), "cpu_used_m": (cpu if cpu is not None else round(a["cpu_used_m"], 1)) if used else None,
                "mem_used_mi": round(a["mem_used_mi"], 1) if used else None, "restarts": 0}

    def traffic_state(self, now):
        """(recorded seconds into the run, replicas, CPU % of target) for the replayed 60 req/s run, or None."""
        if self.traffic_at is None:
            return None
        r = (now - self.traffic_at) * SPEED
        reps = 3 if r < 20 else 4 if r < 50 else 6
        cpu = 10 if r >= 180 else 130 if r < 20 else 112 if r < 50 else 88 if r < 90 else 74
        return r, reps, cpu

    def cluster(self, now):
        c = copy.deepcopy(self.cluster0)
        c["observed_at"], c["operation"], c["traffic"], c["active_jobs"] = iso(now), None, None, []
        desk, lap = c["nodes"][0], c["nodes"][1]
        plan = c["plan"]
        if not self.analyzed:
            c["plan"] = {"run": None, "state": "none", "matched": 0, "total": 0, "ready": False, "workloads": {}}
            return c
        plan.update(state="pending", matched=0, ready=False)
        if self.apply_at is None:
            return c
        t = (now - self.apply_at) * APPLY_SPEED
        old = desk["pods"]
        desk["pods"] = [] if t >= 7 else [dict(p, phase="Terminating") for p in old] if t >= 3 else old
        tr = self.traffic_state(now)
        pods = []
        for app, a in self.after.items():
            n = a["replicas"]
            if app == "checkout-web" and tr:
                n = tr[1]
            for i in range(n):
                fresh = app == "checkout-web" and tr and i >= 3 and tr[0] < (25 if i == 3 else 55)
                cpu = round(a["cpu_request_m"] * tr[2] / 100, 1) if app == "checkout-web" and tr else None
                pods.append(self._pod(app, i, "Pending" if t < 2.5 or fresh else "Running", used=t >= 4 and not fresh, cpu=cpu))
        lap["pods"] = pods
        if tr and tr[0] < 180:
            lap["cpu_used_m"] = 4400.0
        if t >= 7:
            plan.update(state="applied", matched=plan.get("total", 5), ready=True)
            for w in plan.get("workloads", {}).values():
                for chk in w.get("checks", []):
                    chk.update(actual=chk.get("expected"), match=True)
                w["state"] = "applied"
        elif t >= 3:
            plan.update(state="partial", matched=3)
        if t >= 1:
            for app, a in self.after.items():
                d = c["deployments"].get(app)
                if d:
                    d.update(replicas=a["replicas"], ready=a["replicas"] if t >= 7 else 0, node_selector={"kubesage.io/sku": "F8s_v2"},
                             cpu_request_m=float(a["cpu_request_m"]), mem_request_mi=float(a["mem_request_mi"]))
            web, rw = self.after["checkout-web"]["scaling"], self.after["report-worker"]["scaling"]
            c["scaling"] = {
                "checkout-web": {"min": web["min_replicas"], "max": web["max_replicas"], "triggers": ["cron", "cpu"],
                                 "cpu_target": float(web["cpu_utilization_target"]), "current": tr[1] if tr else 3,
                                 "desired": tr[1] if tr else 3, "cpu": tr[2] if tr else 9, "conditions": []},
                "report-worker": {"min": 0, "max": rw["max_replicas"], "triggers": ["cron"]}}
        if tr:
            r = tr[0]
            job = self.job
            if r < 180:
                c["active_jobs"] = [{"name": job, "ends_at": iso(self.traffic_at + TRAFFIC_S)}]
            failed = 23 if r >= 80 else int(23 * r / 80)
            done = r >= 180
            p50, p95 = ((1200, 15000) if r < 30 else (420, 4200) if r < 60 else (120, 900) if r < 90
                        else (75, 170 + int(r * 7) % 70))
            c["traffic"] = {"job": job, "status": "completed" if done else "running",
                            "succeeded_requests": 10777 if done else max(0, int(60 * r) - failed), "failed_requests": failed,
                            "p50_ms": 75 if done else p50, "p95_ms": 210 if done else p95, "elapsed": 180 if done else int(r),
                            "logs_available": True}
        return c

    # ---- the agent team ----
    def tick(self, now):
        if self.tc_at is not None and self.alert_at is None and now - self.tc_at >= ALERT_S:
            self.alert_at = now
            self.emit(now, "alerts", self.alert_text)
            self.emit(now, "agent/team", f"alert ContainerOOMKilled fired: the team runs in {TEAM_WAIT_S:g} s (45 s on the cluster)")
            if self.auto:
                self.pending_until = now + TEAM_WAIT_S
        if self.pending_until is not None and now >= self.pending_until and self.run_at is None:
            self.start_run(now)

    def start_run(self, now):
        self.run_at, self.pending_until = now, None
        for x in self.run_lines:
            self.emit(now + (x["ts"] - self.t0), x["stream"], x["text"])

    def shift(self, s):
        return iso(self.run_at + (epoch(s) - self.t0)) if s else s

    def run(self, now):
        if self.run_at is None:
            return None
        tau, f = now - self.run_at, copy.deepcopy(self.final)
        dur = epoch(f["finished_at"]) - self.t0
        f["started_at"], f["finished_at"] = self.shift(f["started_at"]), self.shift(f["finished_at"])
        done = set()
        for n, a in f["agents"].items():
            start = epoch(a["started"]) - self.t0 + (0.4 if a["role"] == "subagent" else 0)
            end = start + (a["ms"] or 0) / 1000
            a["started"] = self.shift(a["started"])
            if tau >= end:
                done.add(n)
                continue
            keep = {k: a[k] for k in ("title", "role", "size", "label", "model", "description")}
            f["agents"][n] = keep | {"state": "running" if tau >= start else "queued", "started": a["started"] if tau >= start else None,
                                     "ms": None, "signals": None, "findings": None, "cost_usd": 0.0, "tokens_in": 0,
                                     "tokens_out": 0, "error": None, "summary": None}
        f["reports"] = {n: r for n, r in f["reports"].items() if n in done}
        f["signals"] = {n: s for n, s in f["signals"].items() if n in done}
        f["cost_usd"] = round(sum(a["cost_usd"] for n, a in f["agents"].items() if n in done), 6)
        if "thinker" not in done:
            f["diagnosis"] = None
        if "executor" not in done:
            ex = f["agents"]["executor"]
            f["executor"] = None if ex["state"] == "queued" else {
                "base": "main", "branch": None, "commit": None, "edits": [], "rejected": [], "skipped": [], "title": None,
                "body": None, "diff": "", "pr_url": None, "applied": None, "files": []}
        if tau < dur:
            f.update(state="running", finished_at=None)
        return f

    def agents(self, now):
        run = self.run(now)
        pending = None
        if self.alert_at is not None and self.run_at is None and self.pending_until is not None:
            pending = {"alert": "ContainerOOMKilled", "in_s": max(0, round(self.pending_until - now))}
        runs = [{"id": run["id"], "trigger": run["trigger"], "state": run["state"], "mode": run["mode"], "cost_usd": run["cost_usd"],
                 "started_at": run["started_at"], "issues": len((run.get("diagnosis") or {}).get("issues", [])),
                 "pr_url": None, "branch": (run.get("executor") or {}).get("branch")}] if run else []
        return {"team": self.team_info, "run": run, "runs": runs, "auto": self.auto, "prs": False, "pending": pending}

    def testcases(self, now):
        out = copy.deepcopy(self.tcs)
        out["active"], out["alerts"], out["cluster"] = {}, [], True
        if self.tc_at is not None:
            out["active"]["memory"] = {"pods": 1, "phases": {"Running": 1}, "restarts": int((now - self.tc_at) // 5),
                                       "since": iso(self.tc_at), "stopping": False}
        if self.alert_at is not None and self.tc_at is not None:
            out["alerts"] = [{"name": "ContainerOOMKilled", "severity": "critical",
                              "summary": "ledger-cache-6c47d5ccc7-kfr6x (ledger-cache) was OOM-killed in the last 10 minutes",
                              "since": iso(self.alert_at), "pod": "ledger-cache-6c47d5ccc7-kfr6x", "node": None}]
        return out

    # ---- the ring ----
    def emit(self, ts, stream, text):
        self.ring.append({"ts": ts, "stream": stream, "text": text})
        self.ring.sort(key=lambda r: r["ts"])

    def ring_read(self, now, q):
        shown = [dict(r, seq=i + 1) for i, r in enumerate(x for x in self.ring if x["ts"] <= now)]
        want = [p for p in q.get("streams", "").split(",") if p]
        match = [r for r in shown if not want or any(r["stream"].startswith(p.rstrip("*")) for p in want)]
        if "after" in q:
            match = [r for r in match if r["seq"] > int(q["after"])][: int(q.get("limit", 1000))]
        else:
            match = match[-int(q.get("tail", 200)):]
        return {"records": match, "last": len(shown)}

    # ---- requests ----
    def get(self, path, q):
        now = time.time()
        with self.lock:
            self.tick(now)
            if path == "/api/config":
                return 200, self.config
            if path == "/api/cluster":
                return 200, self.cluster(now)
            if path == "/api/reports":
                return 200, self.reports if self.analyzed else {"runs": []}
            if path == "/api/report":
                return (200, self.report) if self.analyzed else (404, {"error": "no analysis yet"})
            if path == "/api/trace":
                return (200, self.trace) if self.analyzed else (404, {"error": "no such run"})
            if path == "/api/agents":
                return 200, self.agents(now)
            if path == "/api/agents/run":
                run = self.run(now)
                return (200, run) if run else (404, {"error": "no such team run"})
            if path == "/api/testcases":
                return 200, self.testcases(now)
            if path == "/api/ring":
                return 200, self.ring_read(now, q)
            if path == "/api/ring/stat":
                n = len([x for x in self.ring if x["ts"] <= now])
                return 200, {"used_bytes": 4_900_000 + n * 120, "capacity_bytes": 500 * 2 ** 20, "records": 51_600 + n,
                             "wrapped": False, "oldest_ts": self.started - 86400}
            return 404, {"error": f"not in the replay: {path}"}

    def post(self, path, body):
        now = time.time()
        if path == "/api/analyze":
            time.sleep(ANALYZE_S)
            with self.lock:
                self.analyzed = True
            return 200, self.report
        with self.lock:
            if path == "/api/apply":
                dry = bool(body.get("dry_run"))
                files = [m for w in self.report["report"]["workloads"] for m in (w.get("manifests") or [])]
                steps = [{"file": f, "ok": True, "skipped": None,
                          "output": f"{'deployment.apps' if 'patch' in f else 'scaledobject.keda.sh'}/{f.split('.')[0]} "
                                    f"{'configured (server dry run)' if dry else 'configured'}"} for f in files]
                if not dry and self.apply_at is None:
                    self.apply_at = now
                return 200, {"run": body.get("run"), "ok": True, "dry_run": dry, "steps": steps}
            if path == "/api/spike":
                self.traffic_at, self.job = now, f"kubesage-spike-{int(now) % 1000:03d}"
                return 200, {"job": self.job, "RPS": str(body.get("rps", 60)), "SECONDS": str(TRAFFIC_S),
                             "WORK_MS": str(body.get("work_ms", 50)), "TARGET": "checkout-web"}
            if path == "/api/testcase":
                if body.get("action") == "start":
                    self.tc_at = now
                    self.emit(now, "testcase/memory", "━━ starting test case memory: ledger-cache loads 700 MiB of ledger "
                                                      "into RAM under a 512 MiB limit, so it is OOM-killed and restarts.")
                    self.emit(now + 0.2, "testcase/memory", "$ kubectl apply -k gitops/apps/ledger-cache -> ok")
                    for i, line in enumerate(self.ledger_lines):
                        self.emit(now + 2 + i * 0.6, "testcase/memory", line)
                    return 200, {"ok": True, "steps": [
                        {"cmd": "kubectl apply -f gitops/apps/namespace.yaml", "ok": True, "output": "namespace/lab unchanged"},
                        {"cmd": "kubectl apply -k gitops/apps/ledger-cache", "ok": True,
                         "output": "configmap/ledger-cache-src created\ndeployment.apps/ledger-cache created"}]}
                self.tc_at = self.alert_at = None
                return 200, {"ok": True, "steps": [{"cmd": "kubectl delete -k gitops/apps/ledger-cache", "ok": True, "output": ""}]}
            if path == "/api/agents/run":
                if self.run_at is None:
                    self.start_run(now)
                return 202, {"id": self.final["id"]}
            if path == "/api/agents/auto":
                self.auto = bool(body.get("on"))
                return 200, {"auto": self.auto}
            if path == "/api/agents/apply":
                dry = bool(body.get("dry_run", True))
                return 200, {"ok": True, "dry_run": dry, "steps": [
                    {"cmd": f"kubectl apply -k gitops/apps/ledger-cache{' --dry-run=server' if dry else ''}", "ok": True,
                     "output": "deployment.apps/ledger-cache configured (server dry run)"},
                    {"cmd": f"kubectl apply -f gitops/apps/namespace.yaml{' --dry-run=server' if dry else ''}", "ok": True,
                     "output": "namespace/lab configured (server dry run)"}],
                    "skipped": [{"path": "gitops/infra/nodepools.tf",
                                 "reason": "Terraform: a node-pool proposal; it needs a separate `terraform apply`"}]}
        return 404, {"error": f"not in the replay: {path}"}


SIM = Sim()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DASHBOARD), **kw)

    def translate_path(self, path):
        url = unquote(urlsplit(path).path)
        if url.startswith("/demo/"):  # the slides and their pictures
            target = (REPO / url.lstrip("/")).resolve()
            return str(target) if target.is_relative_to(DEMO) else str(DEMO / "missing")
        return super().translate_path(path)

    def log_message(self, *a):
        pass

    def _json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            self._json(*SIM.get(url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))
        else:
            super().do_GET()

    def do_POST(self):
        url = urlsplit(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self._json(*SIM.post(url.path, body))


def serve(port=8700):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve()
    print("replay on http://127.0.0.1:8700")
    threading.Event().wait()
