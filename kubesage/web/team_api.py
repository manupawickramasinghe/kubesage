"""The web API for the agent team, the live test cases and the RAM ring (the tmux view's source).

Nothing here gives the browser a terminal. The ring is read-only over HTTP; test cases and team runs are fixed
actions with validated names. Real terminal output reaches the ring from processes the server starts itself
(`kubectl logs -f`, `kubectl get -w`) and from tmux panes piped with `kubesage ring write`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from kubesage import journal, lab, ring
from kubesage.agents import policy, repo, team
from kubesage.agents.loader import TeamError, load_team
from kubesage.apply import kubectl_base

STREAM = re.compile(r"[A-Za-z0-9_./*:-]{1,64}")


class TeamApi:
    def __init__(self, *, vm: str, alertmanager: str, out_dir: Path, mode: str, prs: bool, auto: bool = True):
        self.vm, self.alertmanager, self.mode, self.prs = vm, alertmanager.rstrip("/"), mode, prs
        self.out_dir = Path(out_dir) / "agents"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.ring = ring.shared()
        self.access: deque[dict] = deque(maxlen=5000)
        self.auto = auto
        self._run_lock = threading.Lock()
        self._lab_lock = threading.Lock()
        self.current: team.TeamRun | None = None
        self._alerts_seen: dict[str, tuple[float, str]] = {}  # fingerprint: (first seen, alert name)
        self._alerts: list[dict] = []
        self._pending_trigger: tuple[float, str] | None = None
        self._last_auto = 0.0
        self._followed: dict[str, subprocess.Popen] = {}

    # ---- background ----
    def start_background(self) -> None:
        for name, target in (("alert-watch", self._watch_alerts), ("lab-logs", self._follow_lab),
                             ("lab-watch", self._watch_lab)):
            threading.Thread(target=target, name=name, daemon=True).start()

    def _settings(self) -> team.TeamSettings:
        return team.TeamSettings(vm=self.vm, alertmanager=self.alertmanager, out_dir=self.out_dir, mode=self.mode,
                                 access_log=lambda: list(self.access), open_pr=self.prs)

    def _watch_alerts(self) -> None:
        while True:
            time.sleep(10)
            try:
                alerts = httpx.get(f"{self.alertmanager}/api/v2/alerts?active=true&silenced=false", timeout=5).json()
            except Exception:
                continue
            self._alerts_tick(alerts)

    def _alerts_tick(self, alerts: list[dict]) -> None:
        lab_alerts = [a for a in alerts if a.get("labels", {}).get("namespace") in (None, lab.NAMESPACE)]
        self._alerts = lab_alerts
        active = {a.get("fingerprint") for a in lab_alerts}
        # Forget what resolved, so the same alert firing again later is news (and can trigger the team) again.
        for fp, name in [(fp, name) for fp, (_, name) in self._alerts_seen.items() if fp not in active]:
            del self._alerts_seen[fp]
            ring.emit("alerts", f"RESOLVED {name}")
        fresh = [a for a in lab_alerts if a.get("fingerprint") not in self._alerts_seen]
        for a in fresh:
            labels = a.get("labels", {})
            self._alerts_seen[a.get("fingerprint")] = (time.time(), labels.get("alertname", "alert"))
            text = f"FIRING {labels.get('alertname')} [{labels.get('severity', '-')}] {a.get('annotations', {}).get('summary', '')}"
            ring.emit("alerts", text)
            journal.log("alertmanager", text, level=logging.WARNING)
        if fresh and self.auto and self._pending_trigger is None and time.time() - self._last_auto > 180:
            # Wait for the related alerts to fire too, so the team sees the whole picture.
            self._pending_trigger = (time.time() + 45, fresh[0]["labels"].get("alertname", "alert"))
            ring.emit("agent/team", f"alert {self._pending_trigger[1]} fired: the team runs in 45 s")
        if self._pending_trigger and time.time() >= self._pending_trigger[0]:
            name = self._pending_trigger[1]
            self._pending_trigger = None
            self._last_auto = time.time()
            self.run({"trigger": f"alert:{name}"})

    def _kubectl_lines(self, args: list[str], stream: str) -> int:
        """Run a kubectl that prints lines (logs -f, get -w) and pipe each line into the ring."""
        proc = subprocess.Popen([*kubectl_base(), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in proc.stdout:
                ring.emit(stream, line.rstrip("\n"))
        finally:
            proc.kill()
            proc.wait()
        return proc.returncode or 0

    def _watch_lab(self) -> None:
        def watch(args, stream):
            while True:
                self._kubectl_lines(args, stream)
                time.sleep(5)
        threading.Thread(target=watch, args=(["-n", lab.NAMESPACE, "get", "pods", "-o", "wide", "--watch-only", "-w"],
                                             "watch/pods"), daemon=True).start()
        watch(["-n", lab.NAMESPACE, "get", "events", "--watch-only", "-w"], "watch/events")

    def _follow_lab(self) -> None:
        apps = {tc.app: tc.name for tc in lab.CATALOG if tc.app not in ("batch-fanout", "sidecar-swarm")}
        apps |= {f"{tc.app}-load": tc.name for tc in lab.CATALOG if tc.load_rps}
        while True:
            time.sleep(5)
            pods = lab.lab_pods() or {"items": []}
            for pod in pods["items"]:
                name = pod["metadata"]["name"]
                app = pod["metadata"].get("labels", {}).get("app", "")
                running = pod.get("status", {}).get("phase") == "Running" and not pod["metadata"].get("deletionTimestamp")
                if app in apps and running and name not in self._followed:
                    short = name.rsplit("-", 1)[-1]
                    def follow(name=name, stream=f"testcase/{apps[app]}", short=short, app=app):
                        try:
                            proc = subprocess.Popen([*kubectl_base(), "-n", lab.NAMESPACE, "logs", "-f", name, "--tail=5"],
                                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                            self._followed[name] = proc
                            for line in proc.stdout:
                                ring.emit(stream, f"{app}/{short} {line.rstrip()}")
                        finally:
                            self._followed.pop(name, None)
                    self._followed[name] = None
                    threading.Thread(target=follow, daemon=True).start()

    # ---- test cases ----
    def testcases(self) -> tuple[int, dict]:
        pods = lab.lab_pods()
        return 200, {"catalog": [tc.as_dict() for tc in lab.CATALOG], "active": lab.status(pods),
                     "cluster": pods is not None, "alerts": [
                         {"name": a["labels"].get("alertname"), "severity": a["labels"].get("severity"),
                          "summary": a.get("annotations", {}).get("summary"), "since": a.get("startsAt"),
                          "pod": a["labels"].get("pod"), "node": a["labels"].get("node")} for a in self._alerts]}

    def testcase(self, body: dict) -> tuple[int, dict]:
        action, name = body.get("action"), body.get("name")
        if action not in ("start", "stop", "stop-all"):
            return 400, {"error": "action must be start, stop or stop-all"}
        if action != "stop-all" and name not in lab.BY_NAME:
            return 400, {"error": f"unknown test case; one of {', '.join(lab.BY_NAME)}"}
        if not self._lab_lock.acquire(blocking=False):
            return 409, {"error": "another test case is starting or stopping; wait a moment"}
        try:
            cases = list(lab.CATALOG) if action == "stop-all" else [lab.BY_NAME[name]]
            steps = []
            for tc in cases:
                if action == "start":
                    ring.emit(f"testcase/{tc.name}", f"━━ starting test case {tc.name}: {tc.summary}")
                    steps += lab.start(tc)
                elif action == "stop-all" and tc.name not in lab.status(lab.lab_pods()):
                    continue
                else:
                    steps += lab.stop(tc)
                    ring.emit(f"testcase/{tc.name}", f"━━ stopped test case {tc.name}")
            for s in steps:
                ring.emit(f"testcase/{name or 'all'}", f"$ {s['cmd']} -> {'ok' if s['ok'] else 'FAILED'}")
                for line in s["output"].splitlines()[-6:]:
                    ring.emit(f"testcase/{name or 'all'}", f"  {line}")
                journal.log("testcase", f"$ {s['cmd']} -> {'ok' if s['ok'] else s['output'][-160:]}",
                            level=logging.INFO if s["ok"] else logging.WARNING)
            ok = all(s["ok"] for s in steps)
            return (200 if ok else 502), {"ok": ok, "steps": steps,
                                          "error": None if ok else next(s["output"] for s in steps if not s["ok"])[-400:]}
        finally:
            self._lab_lock.release()

    # ---- agent team ----
    def overview(self) -> tuple[int, dict]:
        try:
            t = load_team()
            team_info = {"profile": t.profile, "provider": {"base_url": t.provider.base_url, "api_key_env": t.provider.api_key_env},
                         "agents": [{"name": a.name, "title": a.config.title or a.name, "role": a.config.role,
                                     "size": a.config.model, "label": a.tier.label or a.tier.model, "model": a.tier.model,
                                     "description": a.config.description, "folder": f"agents/{a.dir.name}"} for a in t.agents],
                         "rules": [{"file": f"agents/rules/{n}", "text": text} for n, text in t.rules], "error": None}
        except (TeamError, OSError, ValueError) as exc:
            team_info = {"error": str(exc), "agents": [], "rules": []}
        current = self.current.snapshot() if self.current else None
        runs = team.list_runs(self.out_dir)
        if current is None and runs:
            try:
                current = team.load_run(self.out_dir, runs[0]["id"])
            except (OSError, ValueError):
                current = None
        return 200, {"team": team_info, "run": current, "runs": runs, "auto": self.auto, "prs": self.prs,
                     "pending": {"alert": self._pending_trigger[1], "in_s": max(0, round(self._pending_trigger[0] - time.time()))}
                     if self._pending_trigger else None}

    def run(self, body: dict) -> tuple[int, dict]:
        trigger = body.get("trigger", "manual")
        if not isinstance(trigger, str) or not re.fullmatch(r"(manual|alert:[A-Za-z0-9_]{1,64})", trigger):
            return 400, {"error": "invalid trigger"}
        if not self._run_lock.acquire(blocking=False):
            return 409, {"error": "the agent team is already running"}
        try:
            t = load_team()
        except (TeamError, OSError, ValueError) as exc:
            self._run_lock.release()
            return 500, {"error": f"agents/: {exc}"}
        settings = self._settings()
        tr = team.TeamRun(t, trigger, team.resolve_mode(t, settings.mode))
        self.current = tr

        def work():
            try:
                team.run(t, settings, trigger=trigger, prepared=tr)
            except Exception as exc:  # surface it in the run instead of a dead thread
                error = f"{type(exc).__name__}: {exc}"
                for name, a in tr.snapshot()["agents"].items():
                    if a["state"] == "running":
                        tr.agent(name, state="failed", error=error[:300])
                tr.set(state="failed", error=error, finished_at=team._now())
                ring.emit("agent/team", f"✗ team run failed: {exc}")
            finally:
                self._run_lock.release()
        threading.Thread(target=work, name=f"team-{tr.id}", daemon=True).start()
        return 202, {"id": tr.id}

    def get_run(self, run_id: str | None) -> tuple[int, dict]:
        if self.current and (run_id in (None, self.current.id)):
            return 200, self.current.snapshot()
        try:
            return 200, team.load_run(self.out_dir, run_id or "")
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except OSError:
            return 404, {"error": "no such team run"}

    def _run_data(self, run_id) -> dict:
        if self.current and run_id == self.current.id:
            return self.current.data
        return team.load_run(self.out_dir, run_id)

    def pr(self, body: dict) -> tuple[int, dict]:
        if not self.prs:
            return 403, {"error": "pull requests are off; start the server with --agent-prs to let the Executor open them"}
        if not self._run_lock.acquire(blocking=False):
            return 409, {"error": "the agent team is running; wait for it to finish"}
        try:
            data = self._run_data(body.get("id"))
            url = team.open_pr(data, self._settings(), self.out_dir / data["id"])
            self._save(data)
            return 200, {"pr_url": url}
        except (ValueError, OSError) as exc:
            return 400, {"error": str(exc)}
        except repo.RepoError as exc:
            return 502, {"error": str(exc)}
        finally:
            self._run_lock.release()

    def apply(self, body: dict) -> tuple[int, dict]:
        """kubectl apply the Executor's commit: each changed app directory, plus the namespace if it changed."""
        dry = body.get("dry_run", True)
        if not isinstance(dry, bool):
            return 400, {"error": "dry_run must be true or false"}
        if not self._run_lock.acquire(blocking=False):
            return 409, {"error": "the agent team is running; wait for it to finish"}
        try:
            data = self._run_data(body.get("id"))
            ex = data.get("executor") or {}
            if not ex.get("commit"):
                return 400, {"error": "this run committed nothing to apply"}
            clone = self.out_dir / "repo"
            repo.git(clone, "checkout", "--quiet", "--detach", ex["commit"])
            targets, skipped = [], []
            for path in ex.get("files", []):
                parts = path.split("/")
                if parts[:2] == ["gitops", "apps"] and len(parts) > 3:
                    targets.append(("-k", str(clone / "gitops" / "apps" / parts[2]), f"gitops/apps/{parts[2]}"))
                elif path == "gitops/apps/namespace.yaml":
                    targets.append(("-f", str(clone / path), path))
                else:
                    skipped.append({"path": path, "reason": "Terraform: needs `terraform apply` against Azure; the home lab can't change hardware"})
            targets = list(dict.fromkeys(targets))
            # The commit passed these checks when it was made; check again what would actually be sent, so a run
            # from an older KubeSage, or a clone changed by hand, can't push something outside the lab.
            problems = []
            for flag, where, shown in targets:
                try:
                    problems += [f"{shown}: {x}" for x in policy.check(policy.render(Path(where)), [lab.NAMESPACE], self._nodes())]
                except policy.PolicyError as exc:
                    problems.append(f"{shown}: {exc}")
            if problems:
                ring.emit("agent/executor", f"✗ apply refused: {problems[0][:200]}")
                journal.log("executor", f"apply refused: {'; '.join(problems)[:300]}", level=logging.WARNING)
                return 422, {"error": "refused: " + "; ".join(problems)[:800], "problems": problems}
            steps = []
            for flag, where, shown in targets:
                cmd = [*kubectl_base(), "apply", flag, where, *(["--dry-run=server"] if dry else [])]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                out = (proc.stdout + proc.stderr).strip()
                steps.append({"cmd": f"kubectl apply {flag} {shown}{' --dry-run=server' if dry else ''}", "ok": proc.returncode == 0,
                              "output": out[-1500:]})
                ring.emit("agent/executor", f"$ kubectl apply {flag} {shown}{' --dry-run=server' if dry else ''}")
                for line in out.splitlines()[-8:]:
                    ring.emit("agent/executor", f"  {line}")
                journal.log("executor", f"apply {shown}{' (dry run)' if dry else ''} -> {'ok' if proc.returncode == 0 else out[-160:]}",
                            level=logging.INFO if proc.returncode == 0 else logging.WARNING)
            ok = all(s["ok"] for s in steps)
            if not dry:
                ex["applied"] = {"at": time.time(), "ok": ok}
                self._save(data)
            return (200 if ok else 502), {"ok": ok, "dry_run": dry, "steps": steps, "skipped": skipped}
        except (ValueError, OSError) as exc:
            return 400, {"error": str(exc)}
        except (repo.RepoError, subprocess.TimeoutExpired) as exc:
            return 502, {"error": str(exc)}
        finally:
            self._run_lock.release()

    def _save(self, data: dict) -> None:
        """Persist a change to a run (Apply, PR) now, the current run included, so a restart doesn't lose it."""
        if self.current and self.current.id == data["id"]:
            data = self.current.snapshot()
        team.save_run(self.out_dir / data["id"], data)

    def _nodes(self) -> list[dict] | None:
        try:
            proc = subprocess.run([*kubectl_base(), "--request-timeout=10s", "get", "nodes", "-o", "json"],
                                  capture_output=True, text=True, timeout=20)
            items = json.loads(proc.stdout).get("items", []) if proc.returncode == 0 else []
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        return [{"pool": n["metadata"].get("labels", {}).get("kubesage.io/pool"), "allocatable": n["status"]["allocatable"]}
                for n in items] or None

    def set_auto(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body.get("on"), bool):
            return 400, {"error": "on must be true or false"}
        self.auto = body["on"]
        if not self.auto:
            self._pending_trigger = None
        journal.log("agents", f"auto-run on alerts {'on' if self.auto else 'off'}")
        return 200, {"auto": self.auto}

    # ---- ring ----
    def _streams(self, raw: str | None) -> list[str] | None:
        if not raw:
            return None
        streams = [s for s in raw.split(",") if s]
        if len(streams) > 20 or not all(STREAM.fullmatch(s) for s in streams):
            raise ValueError("streams must be a comma-separated list of stream names")
        return streams

    def ring_read(self, after: str | None, streams: str | None, limit: str | None, tail: str | None) -> tuple[int, dict]:
        if self.ring is None:
            return 503, {"error": "the RAM ring is not available (is /dev/shm writable?)"}
        try:
            want = self._streams(streams)
            n = min(int(limit or 2000), 5000)
            if tail:
                recs, last = self.ring.tail(min(int(tail), 5000), streams=want)
            else:
                recs, last = self.ring.read(int(after or 0), limit=n, streams=want)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 200, {"records": [r.as_dict() for r in recs], "last": last}

    def ring_stat(self) -> tuple[int, dict]:
        if self.ring is None:
            return 503, {"error": "the RAM ring is not available"}
        return 200, self.ring.stat()

    def ring_dump(self, minutes: str | None, streams: str | None):
        """A generator of text lines for the Save button: the last `minutes` of the ring (0: all of it)."""
        if self.ring is None:
            raise ValueError("the RAM ring is not available")
        mins = int(minutes or 0)
        if not 0 <= mins <= 7 * 24 * 60:
            raise ValueError("minutes must be from 0 to 10080")
        want = self._streams(streams)
        since = time.time() - mins * 60 if mins else 0
        return (ring.format_line(r) + "\n" for r in self.ring.iter_since(since, streams=want))

    def record_access(self, client: str, method: str, path: str, status: int, agent: str, refused: str | None = None) -> None:
        entry = {"at": time.time(), "client": client, "method": method, "path": path[:200], "status": status,
                 "agent": agent[:120], "refused": refused}
        self.access.append(entry)
        if status in (403, 404) or refused:
            ring.emit("access", f"{client} {method} {path[:120]} -> {status}{' refused: ' + refused[:80] if refused else ''}")
