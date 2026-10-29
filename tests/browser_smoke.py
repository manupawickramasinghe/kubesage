"""Optional UI regression checks with mocked APIs; no cluster or LLM calls.

Run after installing Playwright and a browser, for example:
    .venv/bin/pip install playwright
    .venv/bin/python tests/browser_smoke.py --output-dir /tmp/kubesage-browser

Use --executable /path/to/chromium when using an existing browser installation.
This is deliberately separate from pytest so the normal suite needs no browser.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conftest import result, stats  # noqa: E402; reuse the deterministic pipeline fixture
from playwright.sync_api import expect, sync_playwright  # noqa: E402

LATEST = "20260102-000000"
NEWER = "20260103-000000"
OLDER = "20260101-000000"


def fixtures():
    report = json.loads(result.__wrapped__(stats.__wrapped__()).model_dump_json())
    old = copy.deepcopy(report)
    old["generated_at"] = "2026-01-01T00:00:00+00:00"
    pods = []
    for row in report["stats"]:
        for replica in range(row["replicas"]):
            pods.append({"name": f"{row['name']}-abcdef-{replica}", "app": row["name"], "phase": "Running",
                         "ready": True, "restarts": row["restarts"], "cpu_request_m": row["cpu_request_cores"] * 1000,
                         "mem_request_mi": row["mem_request_mib"], "mem_limit_mi": row["mem_request_mib"] * 2,
                         "cpu_used_m": row["cpu_pod_p95"] * 1000, "mem_used_mi": row["mem_pod_p99_mib"]})
    common = {"ready": True, "cpu_cores": 8, "mem_mi": 14336, "alloc_cpu_m": 7800,
              "alloc_mem_mi": 13824, "cpu_used_m": 450, "mem_used_mi": 1024, "os": "Ubuntu 26.04 LTS",
              "kernel": "6.17.0", "kubelet": "v1.34.0", "created": "2026-01-01T00:00:00Z"}
    desktop = {**common, "name": "k3d-kubesage-server-0", "host": "pop-os", "role": "control-plane",
               "k3d": True, "cpu_model": "AMD Ryzen 5 8600G with Radeon Graphics", "pool": "D8s_v5",
               "pool_monthly_usd": 280.32, "internal_ip": "192.0.2.1", "pods": pods}
    worker = {**common, "name": "manupa-hnx", "host": "manupa-hnx", "role": "worker", "k3d": False,
              "cpu_model": "AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx", "pool": "F8s_v2",
              "pool_monthly_usd": 246.74, "internal_ip": "192.0.2.2", "pods": []}
    cluster = {"available": True, "metrics": True, "observed_at": datetime.now(timezone.utc).isoformat(),
               "nodes": [desktop, worker], "scaling": {}, "deployments": {}, "active_jobs": [],
               "plan": {"run": LATEST, "state": "pending", "matched": 0, "total": 4,
                        "ready": False, "workloads": {}, "capacity": {"sku": "F8s_v2", "ready": 1, "required": 1}}}
    return report, old, cluster


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class MockApi:
    """Every API request is handled here, including unknown routes and mutations."""

    def __init__(self):
        self.report, self.old, self.cluster = fixtures()
        self.empty = False
        self.requests = []
        self.failures = {}
        self.sequence = 0
        self.team, self.active = team_fixture(), {}
        self.runs, self.analyze_run = [LATEST, OLDER], LATEST
        self.ring = ring_fixture()
        self.gitops = True

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        body = request.post_data_json if request.method == "POST" else None
        self.requests.append((url.path, body))
        assert request.headers.get("x-kubesage") == "1", "API helper must carry CSRF header"
        if url.path in self.failures:
            status, value = self.failures[url.path]
        elif url.path == "/api/config":
            status, value = 200, {"mode": "mock", "namespace": "demo", "gitops": self.gitops}
        elif url.path == "/api/reports":
            status, value = 200, {"runs": [] if self.empty else [{"run": run, "mode": "mock"} for run in self.runs]}
        elif url.path == "/api/report":
            run = parse_qs(url.query).get("run", [LATEST])[0]
            status, value = (404, {"error": "no analysis yet"}) if self.empty else (200, {"run": run, "report": self.old if run == OLDER else self.report})
        elif url.path == "/api/cluster":
            self.sequence += 1
            value = copy.deepcopy(self.cluster)
            if value.get("available"):
                value["nodes"][0]["cpu_used_m"] += self.sequence  # force an actual redraw on each poll
            status = 200
        elif url.path == "/api/analyze":
            self.empty = False
            if self.analyze_run not in self.runs:
                self.runs.insert(0, self.analyze_run)
            status, value = 200, {"run": self.analyze_run, "report": self.report}
        elif url.path == "/api/trace":
            run = parse_qs(url.query).get("run", [LATEST])[0]
            status, value = 200, {"run": run, "mode": "mock", "exchanges": [], "backfill": None,
                                  "kubectl": [{"file": f"{run}.patch.yaml", "cmd": "kubectl patch deployment x", "manifest": ""}]}
        elif url.path == "/api/apply":
            if not body.get("dry_run") and body.get("run") == LATEST:
                self.cluster["plan"].update(state="applied", matched=4, ready=True)
            status, value = 200, {"run": body["run"], "ok": True, "dry_run": body.get("dry_run", False),
                                  "steps": [{"file": "payment-service.patch.yaml", "ok": True, "skipped": None, "output": "configured"}]}
        elif url.path == "/api/spike":
            status, value = 200, {"job": "kubesage-spike-test", "RPS": str(body["rps"]), "SECONDS": str(body["seconds"]),
                                  "WORK_MS": str(body["work_ms"]), "TARGET": "checkout-web"}
            self.cluster["active_jobs"] = [{"name": value["job"], "ends_at": (datetime.now(timezone.utc) + timedelta(seconds=body["seconds"])).isoformat()}]
        elif url.path == "/api/reset":
            self.cluster["active_jobs"] = []
            self.cluster["plan"].update(state="pending", matched=0, ready=False)
            status, value = 200, {"ok": True, "output": []}
        elif url.path == "/api/gitops":
            status, value = 200, {"branch": "kubesage/test", "commit": "abcdef1234567890", "pr_url": "https://example.test/pr/1"}
        elif url.path == "/api/agents":
            status, value = 200, self.team
        elif url.path == "/api/testcases":
            status, value = 200, {"catalog": [{"name": "memory", "title": "High memory", "summary": "OOM", "expect": "more memory"},
                                              {"name": "cpu-code", "title": "High CPU: single-threaded code", "summary": "GIL", "expect": "processes"}],
                                  "active": self.active, "alerts": [{"name": "ContainerCPUBusy", "severity": "warning", "pod": "risk-scorer-1"}],
                                  "cluster": True}
        elif url.path == "/api/agents/run" and request.method == "GET":  # an earlier run, picked in the agent chat
            status, value = 200, older_run_fixture()
        elif url.path in ("/api/testcase", "/api/agents/run"):
            if body.get("action") == "start":
                self.active[body["name"]] = {"pods": 1, "phases": {"Running": 1}, "restarts": 0, "since": "x", "stopping": False}
            status, value = (200, {"ok": True, "steps": [{"cmd": "kubectl apply -k gitops/apps/x", "ok": True, "output": ""}]}
                             if url.path == "/api/testcase" else {"id": "20261028-120000"})
        elif url.path == "/api/ring":  # like the server: a tail of the streams asked for, or what came after
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            want = [p for p in query.get("streams", "").split(",") if p]
            recs = [r for r in self.ring if not want or any(r["stream"].startswith(p) for p in want)]
            recs = [r for r in recs if r["seq"] > int(query["after"])] if "after" in query else recs[-int(query.get("tail", 200)):]
            status, value = 200, {"records": recs, "last": self.ring[-1]["seq"]}
        elif url.path == "/api/ring/stat":
            status, value = 200, {"used_bytes": 2 ** 20, "capacity_bytes": 500 * 2 ** 20, "records": 9, "wrapped": False, "oldest_ts": 1.79e9}
        else:
            status, value = 500, {"error": f"Unexpected mocked API route: {url.path}"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


UNSAFE = '<img src=x onerror="window.__untrusted_html_executed=1">'


RUN_AT = datetime(2026, 10, 28, 11, 57, 9, tzinfo=timezone.utc)  # the fixture run: sub-agents, then Thinker, then Executor


def team_fixture():
    at = lambda s: (RUN_AT + timedelta(seconds=s)).isoformat()
    agent = lambda title, role, size, model, start=0, **kw: {"title": title, "role": role, "size": size, "label": model, "model": model,
                                                             "state": "done", "started": at(start), "ms": 1500, "signals": 5,
                                                             "findings": 1, "cost_usd": 0.003, **kw}
    agents = {"promql": agent("PromQL", "subagent", "small", "qwen-3.8-27b", summary=UNSAFE),
              "resources": agent("System resources", "subagent", "small", "qwen-3.8-27b"),
              "alerts": agent("Alertmanager", "subagent", "small", "qwen-3.8-27b"),
              "security": agent("Security", "subagent", "small", "qwen-3.8-27b"),
              "thinker": agent("Thinker", "thinker", "large", "gpt-oss-120b", 2),
              "executor": agent("Executor", "executor", "medium", "gpt-oss-120b", 4)}
    run = {"id": "20261028-115709", "trigger": "alert:ContainerCPUBusy", "mode": "live", "profile": "cerebras", "state": "done",
           "started_at": at(0), "finished_at": at(6), "cost_usd": 0.018, "agents": agents,
           "rejected": [{"agent": "thinker", "what": "action A2 change_node_sku nodepool/lab", "reason": "SKU 'X99' is not in the catalog"}],
           "signals": {"promql": [{"id": "promql:risk-scorer.cpu_flat",
           "kind": "cpu_flat", "value": 0.96, "unit": "cores", "detail": "flat", "severity": "warning"}]},
           "reports": {"promql": {"summary": UNSAFE, "findings": [{"title": "near one core", "severity": "warning", "category": "cpu",
                                                                     "target": "deployment/lab/risk-scorer", "summary": UNSAFE,
                                                                     "signal_ids": ["promql:risk-scorer.cpu_flat"]}]}},
           "diagnosis": {"summary": "GIL-bound", "issues": [{"id": "I1", "title": "risk-scorer can only use one core", "category": "cpu",
                                                            "severity": "warning", "confidence": 0.9, "root_cause": UNSAFE,
                                                            "evidence": ["promql:risk-scorer.cpu_flat"], "targets": ["deployment/lab/risk-scorer"]}],
                         "actions": [{"id": "A1", "issue": "I1", "type": "code_change", "target": "deployment/lab/risk-scorer",
                                      "summary": "Use a process pool", "params": {}, "rationale": "GIL", "risk": "", "needs_human": False}]},
           "executor": {"base": "main", "branch": "agents/20261028-115709-risk-scorer-cpu", "commit": "7024c5b1aaaa", "title": "Use processes",
                        "edits": [{"path": "gitops/apps/risk-scorer/src/risk_scorer.py", "action": "A1", "by": "model"}],
                        "diff": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-from concurrent.futures import ThreadPoolExecutor\n"
                                "+from concurrent.futures import ProcessPoolExecutor\n", "skipped": [], "rejected": [], "pr_url": None}}
    return {"team": {"profile": "cerebras", "error": None, "agents": [{"name": n, **{k: a[k] for k in ("title", "role", "size", "label", "model")}}
                                                                       for n, a in agents.items()],
                     "rules": [{"file": "agents/rules/platform.txt", "text": "- risk-scorer needs all 3 cores"}]},
            "run": run, "runs": [{"id": run["id"], "trigger": run["trigger"], "state": "done", "started_at": at(0)},
                                 {"id": "20261027-090000", "trigger": "manual", "state": "done", "started_at": "2026-10-27T09:00:00+00:00"}],
            "auto": True, "prs": True, "pending": None}


def older_run_fixture():
    run = copy.deepcopy(team_fixture()["run"])
    run.update(id="20261027-090000", trigger="manual", started_at="2026-10-27T09:00:00+00:00", finished_at="2026-10-27T09:00:20+00:00",
               rejected=[], diagnosis=None, executor=None, state="failed", error="Thinker: the model call failed")
    run["agents"]["thinker"].update(state="failed", error="HTTP 503")
    run["agents"]["executor"].update(state="queued")
    return run


def ring_fixture():
    rec = lambda seq, stream, text: {"seq": seq, "ts": 1.79e9 + seq, "stream": stream, "text": text}
    return [rec(1, "agent/promql", "▶ PromQL: collecting"), rec(2, "agent/thinker", "● I1 [warning] GIL"),
            rec(3, "agent/executor", "  +from concurrent.futures import ProcessPoolExecutor"), rec(4, "agent/executor", UNSAFE),
            rec(5, "testcase/cpu-code", "risk-scorer/abc transactions/s=6,400,000"), rec(6, "watch/pods", "risk-scorer-1 Running"),
            rec(7, "journal", "agents: team run started"), rec(8, "access", "10.0.0.5 GET /.env -> 404"), rec(9, "tmux/ops", "$ kubectl top pods")]


def traffic_fixture(status="completed", errors=0, *, logs=True):
    return {"job": "kubesage-spike-test", "status": status, "succeeded_requests": 500 if logs else None,
            "failed_requests": errors if logs else None, "p50_ms": 120 if logs else None,
            "p95_ms": 7000 if errors and logs else 300 if logs else None,
            "elapsed": 180 if logs else None, "logs_available": logs}


def ready(page, url):
    page.goto(url + "#dashboard")
    expect(page.locator("#mode")).to_have_text("Mock LLM")
    expect(page.locator("#cluster-status")).not_to_have_text("Connecting to the cluster…")
    page.wait_for_function("document.fonts.status === 'loaded'")


def check_layout(page):
    return page.evaluate("""() => {
      const outside = [...document.querySelectorAll('#bars svg text, #flow svg text, #iso .nl-name, #iso .nl-sub')].filter(el => {
        const a=el.getBoundingClientRect(), b=el.closest('svg').getBoundingClientRect();
        return a.left < b.left-3 || a.right > b.right+3;
      }).map(el=>({text:el.textContent,svg:el.closest('svg').getAttribute('aria-label')}));
      return {pageOverflow:document.documentElement.scrollWidth>innerWidth, outside};
    }""")


def run(args):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    handler = partial(QuietHandler, directory=str(ROOT / "dashboard"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    results = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=args.executable)

            def scenario(name, fn, *, width=1400, theme="light"):
                mock = MockApi()
                context = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme,
                                              reduced_motion="reduce", accept_downloads=True)
                context.route("**/api/**", mock.route)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    fn(page, mock)
                    assert not errors, errors
                    results.append({"name": name, "ok": True})
                except Exception as exc:
                    page.screenshot(path=str(output / f"failure-{name}.png"), full_page=True)
                    results.append({"name": name, "ok": False, "error": str(exc), "page_errors": errors})
                finally:
                    context.close()
                print(json.dumps(results[-1]), flush=True)

            def offline(page, mock):
                mock.empty = True
                mock.cluster = {"available": False}
                ready(page, url)
                expect(page.locator("#workloads")).to_contain_text("No analysis yet")
                expect(page.locator("#cluster-status")).to_contain_text("Cluster not reachable")
                for button in ("#btn-apply", "#btn-reset", "#btn-spike", "#btn-export"):
                    expect(page.locator(button)).to_be_disabled()
                expect(page.locator("#btn-analyze")).to_be_enabled()
                page.screenshot(path=str(output / "empty-offline.png"), full_page=True)

            scenario("empty-offline", offline)

            def actions(page, mock):
                ready(page, url)
                expect(page.locator("#step-apply")).not_to_have_class("step done")
                page.locator("#dryrun").check()
                page.locator("#btn-apply").click()
                expect(page.locator("#notice")).to_contain_text("No changes saved")
                expect(page.locator("#step-apply")).not_to_have_class("step done")
                expect(page.locator("#st-apply")).to_contain_text("Validation: 1/1 checked")
                page.locator("#dryrun").uncheck()
                page.locator("#btn-apply").click()
                expect(page.locator("#st-apply")).to_contain_text("Live specs match this plan")
                page.reload()
                expect(page.locator("#st-apply")).to_contain_text("Live specs match this plan")
                page.locator("#secs").fill("10")
                page.locator("#btn-spike").click()
                expect(page.locator("#btn-spike")).to_be_disabled()
                expect(page.locator("#st-spike")).to_contain_text("Traffic running")
                before = page.locator("#st-spike").inner_text()
                expect(page.locator("#st-spike")).not_to_have_text(before, timeout=3000)
                page.locator("#btn-reset").click()
                expect(page.locator("#notice")).to_contain_text("Demo reset")
                expect(page.locator("#st-apply")).not_to_contain_text("Live specs match")
                expect(page.locator("#st-spike")).not_to_contain_text("Traffic running")
                expect(page.locator("#btn-spike")).to_be_enabled()

            scenario("validation-apply-reload-traffic-reset", actions)

            def traffic_result(page, mock, *, status="completed", errors=0, logs=True):
                mock.cluster["traffic"] = traffic_fixture(status, errors, logs=logs)
                ready(page, url)
                result = page.locator("#traffic-result")
                expect(result).to_be_visible()
                if status == "failed" or errors:
                    expect(result).to_have_attribute("data-kind", "err")
                expect(result).to_contain_text("Traffic job failed" if status == "failed" else "Traffic finished")
                if logs:
                    expect(result).to_contain_text(f"500 successful · {errors} failed requests")
                    expect(result).to_contain_text("p95 7,000 ms" if errors else "p95 300 ms")
                else:
                    expect(result).to_contain_text("Request results are unavailable")
                    expect(result).not_to_contain_text("successful")
                    expect(result).not_to_contain_text("0 failed")
                if errors:
                    expect(result).to_contain_text("request errors")
                expect(page.locator("#btn-spike")).to_be_enabled()
                name = f"traffic-{status}-{'unknown' if not logs else 'errors' if errors else 'success'}"
                page.locator("#step-spike").screenshot(path=str(output / f"{name}.png"),
                                                       style="header.top, .skip { visibility: hidden !important; }")

            scenario("traffic-completed-with-request-errors", lambda page, mock: traffic_result(page, mock, errors=20))
            scenario("traffic-completed-zero-errors", traffic_result)
            scenario("traffic-completed-unknown-results", lambda page, mock: traffic_result(page, mock, logs=False))
            scenario("traffic-job-failed", lambda page, mock: traffic_result(page, mock, status="failed", logs=False))

            def aborted_traffic(page, mock):
                ready(page, url)
                page.locator("#btn-spike").click()
                expect(page.locator("#btn-spike")).to_be_disabled()
                expect(page.locator("#st-spike")).to_contain_text("Traffic running")
                mock.cluster["active_jobs"] = []
                mock.cluster["traffic"] = traffic_fixture("failed", logs=False)
                expect(page.locator("#traffic-result")).to_contain_text("Traffic job failed", timeout=5000)
                expect(page.locator("#traffic-result")).to_have_attribute("data-kind", "err")
                expect(page.locator("#st-spike")).not_to_contain_text("Traffic running")
                expect(page.locator("#btn-spike")).to_be_enabled()

            scenario("aborted-job-clears-local-countdown", aborted_traffic)

            def failures(page, mock):
                ready(page, url)
                mock.failures["/api/apply"] = 502, {"error": "Some changes failed; review the step output before retrying.",
                    "steps": [{"file": "api.patch.yaml", "ok": False, "output": "connection refused"}]}
                page.locator("#btn-apply").click()
                expect(page.locator("#notice")).to_have_attribute("role", "alert")
                expect(page.locator("#notice")).to_contain_text("Some changes failed")
                expect(page.locator("#log")).to_contain_text("api.patch.yaml: connection refused")
                expect(page.locator("#st-apply")).not_to_contain_text("Live specs match")
                mock.failures["/api/analyze"] = 502, {"error": "metrics query failed: VictoriaMetrics unavailable"}
                page.locator("#btn-analyze").click()
                expect(page.locator("#notice")).to_contain_text("VictoriaMetrics unavailable")
                expect(page.locator("#btn-analyze")).to_be_enabled()
                expect(page.locator("main")).to_have_attribute("aria-busy", "false")

            scenario("partial-apply-and-analysis-error", failures)

            def history(page, mock):
                ready(page, url)
                page.locator("#report-select").select_option(OLDER)
                expect(page.locator("#snapshot-note")).to_contain_text(OLDER)
                expect(page.locator("#st-apply")).to_contain_text("older run")
                page.locator("#btn-apply").click()
                assert [body for path, body in mock.requests if path == "/api/apply"][-1]["run"] == OLDER
                page.locator("#btn-gitops").click()
                assert [body for path, body in mock.requests if path == "/api/gitops"][-1]["run"] == OLDER
                with page.expect_download() as event:
                    page.locator("#btn-export").click()
                download = event.value
                assert OLDER in download.suggested_filename
                assert json.loads(Path(download.path()).read_text())["generated_at"] == mock.old["generated_at"]
                with page.expect_download() as event:
                    page.locator("#btn-manifests").click()
                text = Path(event.value.path()).read_text()
                assert "kind: Deployment" in text and ".migrate.yaml" in text
                page.locator("#workload-search").fill("payment")
                expect(page.locator("details.wl")).to_have_count(1)
                page.locator("#workload-search").fill("does-not-exist")
                expect(page.locator("#workloads")).to_contain_text("No workloads match")
                page.locator("#workload-search").fill("")
                page.locator("#status-filter").select_option("HEALTHY")
                expect(page.locator("details.wl")).to_have_count(1)
                expect(page.locator("details.wl")).to_contain_text("catalog-api")

            scenario("history-pins-actions-and-downloads-filter", history)

            def focus(page, mock):
                ready(page, url)
                for selector, attribute in (("#legend button", "data-app"), ("#scene [data-pod]", "data-pod"),
                                            ("#scene [data-node]", "data-node"), ("#inspector [data-pick]", "data-pick")):
                    target = page.locator(selector).first
                    identity = target.get_attribute(attribute)
                    target.focus()
                    old_sequence = mock.sequence
                    page.wait_for_function("true")
                    page.wait_for_timeout(3200)
                    assert mock.sequence > old_sequence, "No poll occurred"
                    assert page.evaluate("([a,v])=>document.activeElement.getAttribute(a)===v", [attribute, identity]), selector
                row = page.locator("#bars .brow").first
                row.focus()
                expect(row).to_have_attribute("aria-describedby", "tip")
                row.press("Enter")
                expect(page.locator("#inspector .insp-h")).to_contain_text(row.get_attribute("data-app"))
                page.keyboard.press("Escape")
                expect(page.locator("#inspector .insp-h")).to_contain_text("Whole cluster")

            scenario("keyboard-focus-survives-poll", focus)

            def theme_and_untrusted_text(page, mock):
                unsafe = '<img src=x onerror="window.__untrusted_html_executed=1">'
                mock.cluster["nodes"][0]["host"] = unsafe
                mock.report["workloads"][0]["recommendation"]["diagnosis"] = unsafe
                ready(page, url)
                page.locator("details.wl > summary").first.click()
                expect(page.locator("details.wl").first).to_contain_text(unsafe)
                assert page.locator("img").count() == 0
                assert not page.evaluate("window.__untrusted_html_executed")
                page.locator("#btn-theme").click()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                page.reload()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                expect(page.locator("#btn-theme")).to_have_attribute("aria-label", "Switch to light theme")

            scenario("theme-persistence-and-untrusted-text", theme_and_untrusted_text)

            def agent_team(page, mock):
                page.goto(url + "#agents")
                expect(page.locator("#team-flow .agent")).to_have_count(6)
                expect(page.locator("#team-detail .diff .add")).to_contain_text("ProcessPoolExecutor")
                page.locator('[data-agent="promql"]').click()
                expect(page.locator("#team-detail")).to_contain_text(UNSAFE)  # model text is shown as text
                page.locator("#tc-select").select_option("cpu-code")
                page.locator("#tc-start").click()
                expect(page.locator("#tc-live")).to_contain_text("cpu-code")
                assert [b for path, b in mock.requests if path == "/api/testcase"][-1] == {"action": "start", "name": "cpu-code"}
                expect(page.locator("#tc-start")).to_be_disabled()
                assert page.locator("img").count() == 0 and not page.evaluate("window.__untrusted_html_executed")

            scenario("agent-team-panel", agent_team)

            def demo_guide(page, mock):
                page.goto(url)
                expect(page.locator("#demo")).to_be_visible()
                expect(page.locator("#demo-acts .ds")).to_have_count(9)
                expect(page.locator("#demo-preflight")).to_contain_text("Mock LLM")
                expect(page.locator("#demo-figs")).to_contain_text("6 agents")
                page.locator("#demo-start").click()
                expect(page.locator("#main")).to_be_visible()
                expect(page.locator("#dock")).to_be_visible()
                expect(page.locator("#dk-count")).to_have_text("1 of 9")
                page.locator("#dk-next").click()
                expect(page.locator("#dk-title")).to_have_text("Find the waste")
                page.locator("#dk-actions [data-run]").click()
                expect(page.locator("#dk-status")).to_contain_text("workloads change")
                assert any(path == "/api/analyze" for path, _ in mock.requests)
                page.locator('#dk-dots [data-step="5"]').click()  # step 6, Break something
                expect(page.locator("#agents")).to_be_visible()
                expect(page.locator("#dk-tc")).to_have_value("cpu-code")  # the demo's default incident
                page.locator("#dk-actions [data-run]").click()
                expect(page.locator("#tc-live")).to_contain_text("cpu-code")
                assert [b for path, b in mock.requests if path == "/api/testcase"][-1] == {"action": "start", "name": "cpu-code"}
                page.locator("#dk-next").click()
                page.locator("#dk-actions [data-run]").click()  # Run agent team
                expect(page.locator("#team-msg")).to_contain_text("20261028-120000")
                page.reload()  # the presenter's place survives a reload
                expect(page.locator("#dk-count")).to_have_text("7 of 9")
                page.locator("#dk-end").click()
                expect(page.locator("#demo")).to_be_visible()
                expect(page.locator("#dock")).to_be_hidden()
                page.goto(url + "#cluster-card")  # a FinOps section link opens the FinOps view
                expect(page.locator("#main")).to_be_visible()

            scenario("demo-guide", demo_guide)

            def demo_phone(page, mock):
                page.goto(url)
                expect(page.locator("#demo-acts .ds")).to_have_count(9)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.locator('#demo-acts [data-go="5"]').click()
                expect(page.locator("#dock")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("#dock").bounding_box()["width"] <= 390

            scenario("demo-phone-width", demo_phone, width=390)

            def tmux_mirror(page, mock):
                page.goto(url + "#tmux")
                expect(page.locator("#tx-win .pane")).to_have_count(6)
                expect(page.locator("#tx-win")).to_contain_text("ProcessPoolExecutor")
                assert page.locator("#tmuxview input, #tmuxview textarea, #tmuxview [contenteditable]").count() == 0
                page.locator('#tx-windows [data-win="3"]').click()
                expect(page.locator("#tx-win .pane")).to_have_count(1)
                expect(page.locator("#tx-win")).to_contain_text("$ kubectl top pods")
                page.locator("#tx-range").select_option("30")
                assert page.locator("#tx-save").get_attribute("href") == "/api/ring/dump?minutes=30"
                assert not any(path.startswith("/api/ring") and b for path, b in mock.requests), "the mirror must never POST"
                assert page.locator("img").count() == 0

            scenario("tmux-read-only-mirror", tmux_mirror)

            def tmux_phone(page, mock):
                page.goto(url + "#tmux")
                expect(page.locator("#tx-win .pane")).to_have_count(6)
                assert all(page.locator("#tx-win .pane").nth(i).bounding_box()["height"] > 200 for i in range(6))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            scenario("tmux-phone-width", tmux_phone, width=390)

            def tmux_beside(page, mock):
                page.goto(url + "#agents")
                page.locator("#chat-x").click()  # the chat would share the width this test measures
                expect(page.locator("#txside")).to_be_hidden()
                page.locator("#btn-side").click()
                expect(page.locator("#txside")).to_be_visible()
                expect(page.locator("#btn-side")).to_have_attribute("aria-pressed", "true")
                expect(page.locator("#txs-win .pane")).to_have_count(6)
                expect(page.locator("#txs-win")).to_contain_text("ProcessPoolExecutor")
                side, agents = page.locator("#txside").bounding_box(), page.locator("#agents").bounding_box()
                assert agents["x"] + agents["width"] <= side["x"] + 1, (agents, side)  # beside the view, not over it
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.locator('#txs-win [data-zoom="2"]').click()  # tmux's prefix z
                expect(page.locator("#txs-win .pane:visible")).to_have_count(1)
                expect(page.locator("#txs-windows")).to_contain_text("0:agents*Z")
                page.locator('#txs-windows [data-win="3"]').click()
                expect(page.locator("#txs-win")).to_contain_text("$ kubectl top pods")
                page.locator("#txs-full").click()  # the full view replaces the panel and keeps its own window
                expect(page.locator("#tmuxview")).to_be_visible()
                expect(page.locator("#txside")).to_be_hidden()
                expect(page.locator("#tx-win .pane")).to_have_count(6)
                expect(page.locator("#tx-side")).to_have_attribute("aria-pressed", "true")
                page.goto(url + "#dashboard")
                expect(page.locator("#txside")).to_be_visible()
                expect(page.locator("#txs-win")).to_contain_text("$ kubectl top pods")
                width = page.locator("#txside").bounding_box()["width"]
                page.locator("#txs-grip").focus()
                page.keyboard.press("ArrowLeft")
                assert page.locator("#txside").bounding_box()["width"] == width + 40
                page.reload()  # open, width and window are remembered
                expect(page.locator("#txside")).to_be_visible()
                expect(page.locator("#txs-windows")).to_contain_text("3:ops*")
                assert page.locator("#txside").bounding_box()["width"] == width + 40
                page.locator("#txs-x").click()
                expect(page.locator("#txside")).to_be_hidden()
                assert page.evaluate("getComputedStyle(document.body).paddingRight") == "0px"
                assert page.locator("#txside input, #txside textarea, #txside [contenteditable]").count() == 0
                assert not any(path.startswith("/api/ring") and b for path, b in mock.requests), "the mirror must never POST"

            scenario("tmux-beside-other-views", tmux_beside)

            def tmux_panel_from_the_tmux_tab(page, mock):
                # The panel hides over the tmux tab, so turning it on there must take you somewhere it shows.
                page.goto(url + "#lab")
                expect(page.locator("#btn-side")).to_contain_text("tmux panel")  # labelled, not a bare icon
                page.goto(url + "#tmux")
                page.locator("#tx-side").click()
                expect(page.locator("#lab")).to_be_visible()
                expect(page.locator("#txside")).to_be_visible()
                expect(page.locator("#txs-win .pane")).to_have_count(6)

            scenario("tmux-panel-from-the-tmux-tab", tmux_panel_from_the_tmux_tab)

            def quiet_panes_keep_their_lines(page, mock):
                # Live: hours of kubectl -w after a team run left every agent pane "nothing in the ring" on first load.
                mock.ring = ring_fixture()[:4] + [{"seq": 10 + i, "ts": 1.79e9 + 10 + i, "stream": "watch/pods",
                                                   "text": f"checkout-web-{i} Running"} for i in range(6000)]
                page.goto(url + "#tmux")
                expect(page.locator("#tx-win .pane").nth(0)).to_contain_text("PromQL: collecting")
                expect(page.locator("#tx-win .pane").nth(5)).to_contain_text("ProcessPoolExecutor")
                page.locator('#tx-windows [data-win="1"]').click()
                expect(page.locator("#tx-win .pane").nth(1)).to_contain_text("checkout-web-5999 Running")

            scenario("quiet-panes-keep-their-lines", quiet_panes_keep_their_lines)

            def tmux_beside_phone(page, mock):
                page.goto(url + "#agents")
                page.locator("#btn-side").click()
                expect(page.locator("#txs-win .pane")).to_have_count(6)
                box = page.locator("#txside").bounding_box()
                assert box["width"] <= 768 and box["y"] > 300, box  # a sheet at the bottom, under the view
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            scenario("tmux-beside-tablet-width", tmux_beside_phone, width=768)

            def header_keeps_its_rows(page, mock):
                # The side button must not push the header onto another row; nor may the panel, by narrowing the page.
                # Without --gitops, as the LAN server runs: its extra button wraps the header at tablet widths anyway.
                mock.gitops = False
                height = lambda: page.evaluate("document.querySelector('header.top').offsetHeight")
                for width in (390, 768, 1280, 1920):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(url + "#agents")
                    expect(page.locator("#team-flow .agent")).to_have_count(6)
                    page.wait_for_function("document.fonts.status === 'loaded'")
                    closed = height()
                    page.evaluate("document.querySelectorAll('#btn-side, #btn-chat').forEach(b => b.style.display = 'none')")
                    assert height() == closed, (width, "the panel buttons add a row", closed, height())
                    page.evaluate("document.querySelectorAll('#btn-side, #btn-chat').forEach(b => b.removeAttribute('style'))")
                    if width > 600:
                        page.locator("#btn-side").click()
                        if page.locator("#chat").is_hidden():
                            page.locator("#btn-chat").click()
                        expect(page.locator("#txside")).to_be_visible()
                        expect(page.locator("#chat")).to_be_visible()
                        assert height() == closed, (width, "the open panels wrap the header", closed, height())
                        page.locator("#txs-x").click()
                        page.locator("#chat-x").click()

            scenario("header-keeps-its-rows", header_keeps_its_rows)

            def agent_chat(page, mock):
                # The left sidebar: what each agent handed the next in the run, beside every view, open by default when wide.
                think = (RUN_AT + timedelta(seconds=3)).timestamp()
                mock.ring = ring_fixture() + [{"seq": 10, "ts": think, "stream": "agent/thinker", "text": "  ⋯ Only one core is busy: the GIL."},
                                              {"seq": 11, "ts": think - 3600, "stream": "agent/thinker", "text": "  ⋯ an older run's thought"}]
                page.goto(url + "#demo")
                log = page.locator("#chat-log")
                expect(page.locator("#chat")).to_be_visible()
                expect(page.locator("#btn-chat")).to_have_attribute("aria-pressed", "true")
                expect(log.locator(".msg").first).to_contain_text("Alert ContainerCPUBusy fired")
                promql = log.locator('[data-msg="sub:promql"]')
                expect(promql).to_contain_text("to Thinker")
                expect(promql).to_contain_text("near one core")
                expect(promql).to_contain_text(UNSAFE)  # model text is shown as text
                expect(log.locator('[data-msg="guard:think"]')).to_contain_text("SKU 'X99' is not in the catalog")
                expect(log.locator('[data-msg="think"]')).to_contain_text("to Executor")
                expect(log.locator('[data-msg="think"]')).to_contain_text("code change deployment/lab/risk-scorer")
                expect(log.locator('[data-msg="exec"]')).to_contain_text("Committed “Use processes”")
                expect(log.locator('[data-msg="exec"]')).to_contain_text("agents/20261028-115709-risk-scorer-cpu")
                expect(log.locator(".msg").last).to_contain_text("Run done in 6 s")
                order = log.locator(".msg").evaluate_all("els => els.map(e => e.dataset.msg)")
                assert order.index("guard:think") < order.index("think") < order.index("exec") < order.index("end"), order
                why = log.locator('[data-msg="think"] .msg-why')
                why.locator("summary").click()
                expect(why.locator("pre")).to_have_text("Only one core is busy: the GIL.")  # this run's window only
                chat, demo = page.locator("#chat").bounding_box(), page.locator("#demo").bounding_box()
                assert chat["x"] + chat["width"] <= demo["x"] + 1, (chat, demo)  # beside the view, not over it
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("img").count() == 0 and not page.evaluate("window.__untrusted_html_executed")
                page.goto(url + "#dashboard")  # the chat keeps the team's state polled on FinOps too
                n = sum(1 for path, _ in mock.requests if path == "/api/agents")
                page.wait_for_timeout(6500)
                assert sum(1 for path, _ in mock.requests if path == "/api/agents") > n, "the chat went stale on FinOps"
                log.locator('[data-msg="sub:promql"] .msg-who').click()  # the sender opens its own report
                expect(page.locator("#agents")).to_be_visible()
                expect(page.locator('#team-flow [data-agent="promql"]')).to_have_attribute("aria-pressed", "true")
                page.locator("#chat-pick").select_option("20261027-090000")
                expect(log.locator('[data-msg="think"]')).to_contain_text("I couldn't decide: HTTP 503")
                expect(log.locator(".msg").last).to_contain_text("Run failed")
                page.locator("#chat-pick").select_option("")
                expect(log.locator('[data-msg="exec"]')).to_be_visible()
                page.locator("#chat-x").click()
                expect(page.locator("#chat")).to_be_hidden()
                assert page.evaluate("getComputedStyle(document.body).paddingLeft") == "0px"
                page.reload()  # closing it is remembered
                expect(page.locator("#team-flow .agent")).to_have_count(6)
                expect(page.locator("#chat")).to_be_hidden()
                page.locator("#btn-chat").click()
                expect(page.locator("#chat")).to_be_visible()
                assert page.locator("#chat input, #chat textarea, #chat [contenteditable]").count() == 0

            scenario("agent-chat-beside-every-view", agent_chat)

            def agent_chat_live(page, mock):
                run = mock.team["run"]
                run.update(state="running", finished_at=None, diagnosis=None, executor=None, rejected=[])
                run["agents"]["thinker"].update(state="running", ms=None)
                run["agents"]["executor"].update(state="queued", ms=None)
                mock.team["pending"] = None
                page.goto(url + "#lab")
                log = page.locator("#chat-log")
                expect(log.locator('[data-msg="typing:thinker"]')).to_contain_text("Thinker is deciding what to do")
                expect(log.locator('[data-msg="sub:security"]')).to_be_visible()
                assert log.locator('[data-msg="think"], [data-msg="end"]').count() == 0
                run["agents"]["thinker"].update(state="done", ms=1500)
                run["diagnosis"] = team_fixture()["run"]["diagnosis"]
                run["agents"]["executor"].update(state="running")
                expect(log.locator('[data-msg="typing:executor"]')).to_contain_text("Executor is writing the change", timeout=10000)
                expect(log.locator('[data-msg="think"]')).to_be_visible()
                assert log.evaluate("el => el.scrollHeight - el.scrollTop - el.clientHeight") < 48, "a new message is out of view"

            scenario("agent-chat-live-run", agent_chat_live)

            def agent_chat_tablet(page, mock):
                page.goto(url + "#agents")
                expect(page.locator("#team-flow .agent")).to_have_count(6)
                expect(page.locator("#chat")).to_be_hidden()  # closed by default when there's no room beside the view
                page.locator("#btn-chat").click()
                expect(page.locator('#chat-log [data-msg="exec"]')).to_be_visible()
                assert page.evaluate("getComputedStyle(document.body).paddingLeft") == "0px"  # over the view, not beside it
                page.locator("#chat-log").focus()
                page.keyboard.press("Escape")
                expect(page.locator("#chat")).to_be_hidden()
                expect(page.locator("#btn-chat")).to_be_focused()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            scenario("agent-chat-tablet-width", agent_chat_tablet, width=768)

            def agent_chat_phone(page, mock):
                page.goto(url + "#agents")
                expect(page.locator("#btn-chat")).to_be_hidden()  # the phone header has no room for it
                page.locator("#team-runbar [data-chat]").click()
                expect(page.locator('#chat-log [data-msg="exec"]')).to_be_visible()
                assert page.locator("#chat").bounding_box()["width"] <= 390 - 40
                page.locator("#chat-x").click()
                expect(page.locator("#chat")).to_be_hidden()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            scenario("agent-chat-phone-width", agent_chat_phone, width=390)

            def agent_chat_empty(page, mock):
                mock.team.update(run=None, runs=[])
                page.goto(url + "#demo")
                expect(page.locator("#chat-log")).to_contain_text("No team run yet")
                page.locator("#chat-run").click()
                assert [b for path, b in mock.requests if path == "/api/agents/run" and b] == [{"trigger": "manual"}]

            scenario("agent-chat-empty", agent_chat_empty)

            def plan_shown_is_plan_applied(page, mock):
                # Review F02: a failed history refresh after Analyze left the old plan on screen while Apply sent the new run.
                ready(page, url)
                mock.analyze_run = NEWER
                mock.failures["/api/reports"] = (500, {"error": "history unavailable"})
                page.locator("#btn-analyze").click()
                expect(page.locator("#report-select")).to_have_value(NEWER)
                expect(page.locator("#log")).to_contain_text("analysis history: history unavailable")
                page.locator("#dryrun").check()
                page.locator("#btn-apply").click()
                expect(page.locator("#notice")).to_contain_text("Validation complete")
                assert [b for path, b in mock.requests if path == "/api/apply"][-1]["run"] == NEWER

            scenario("plan-shown-is-plan-applied", plan_shown_is_plan_applied)

            def errors_reach_the_open_view(page, mock):
                # Review F12: a case-start failure from the tmux view and an Analyze failure from Under the hood were
                # written to message areas in other, hidden views.
                page.goto(url + "#tmux")
                expect(page.locator("#tx-win .pane")).to_have_count(6)
                mock.failures["/api/testcase"] = (502, {"error": "kubectl apply failed: quota exceeded"})
                page.locator("#tx-tc").select_option("cpu-code")
                page.locator("#tx-start").click()
                expect(page.locator("#toast")).to_be_visible()
                expect(page.locator("#toast")).to_contain_text("quota exceeded")
                assert page.locator("#toast").get_attribute("role") == "alert"
                page.locator("#toast-x").click()
                expect(page.locator("#toast")).to_be_hidden()
                page.goto(url + "#lab")
                mock.failures["/api/analyze"] = (502, {"error": "model provider timed out"})
                page.locator("#lab-analyze").click()
                expect(page.locator("#toast")).to_contain_text("model provider timed out")
                page.goto(url + "#dashboard")  # in its own view the inline notice is enough
                page.locator("#toast-x").click()
                page.locator("#btn-analyze").click()
                expect(page.locator("#notice")).to_contain_text("model provider timed out")
                expect(page.locator("#toast")).to_be_hidden()

            scenario("errors-reach-the-open-view", errors_reach_the_open_view)

            def keyboard_stays_put(page, mock):
                # Review F13: re-rendering the team lost focus, and Tab in the shell never let go with text typed.
                page.goto(url + "#agents")
                expect(page.locator("#team-flow .agent")).to_have_count(6)
                page.locator('[data-agent="promql"]').focus()
                page.evaluate("renderTeam()")
                assert page.evaluate("document.activeElement.dataset.agent") == "promql"
                page.goto(url + "#lab")
                shell = page.locator("#sh-in")
                shell.fill("zzz")  # nothing to complete: Tab moves on
                shell.press("Tab")
                assert page.evaluate("document.activeElement.id") != "sh-in"
                shell.fill("kubectl g")  # completes in place
                shell.press("Tab")
                assert page.evaluate("document.activeElement.id") == "sh-in" and shell.input_value() == "kubectl get "
                shell.fill("kubectl ")  # lists the options once, then the next Tab moves on
                shell.press("Tab")
                assert page.evaluate("document.activeElement.id") == "sh-in"
                shell.press("Tab")
                assert page.evaluate("document.activeElement.id") != "sh-in"
                shell.fill("kubectl g")
                shell.press("Shift+Tab")  # never a completion
                assert page.evaluate("document.activeElement.id") != "sh-in" and shell.input_value() == "kubectl g"

            scenario("keyboard-stays-put", keyboard_stays_put)

            def wire_follows_operations(page, mock):
                # Review F14: the Wire reloaded only when the run id changed, so it went stale after a new Analyze or Apply.
                page.goto(url + "#lab")
                expect(page.locator("#wire-kinds")).to_contain_text("kubectl 1")
                traces = lambda: [p for p, _ in mock.requests if p == "/api/trace"]
                first = len(traces())
                mock.analyze_run = NEWER
                page.locator("#lab-analyze").click()
                expect(page.locator("#wire-run")).to_have_value(NEWER)
                page.wait_for_function("lab.traceRun === '%s' && !lab.traceStale" % NEWER)
                after_analyze = len(traces())
                assert after_analyze > first
                page.locator("#lab-apply").click()  # same run, new state: the trace is fetched again
                page.wait_for_function(f"!lab.traceStale")
                expect(page.locator("#wire-run")).to_have_value(NEWER)
                page.wait_for_timeout(200)
                assert len(traces()) > after_analyze

            scenario("wire-follows-operations", wire_follows_operations)

            def unknown_usage_is_not_zero(page, mock):
                # Review F09: one sampled pod of eight showed as low utilisation; an unread inventory looked empty.
                pods = mock.cluster["nodes"][0]["pods"]
                for p in pods[1:]:
                    p["cpu_used_m"] = p["mem_used_mi"] = None
                ready(page, url)
                expect(page.locator("#inspector .big")).to_have_text("—")
                expect(page.locator("#inspector")).to_contain_text(f"1 of {len(pods)} pods sampled")
                page.locator("#iso [data-node]").first.focus()  # a keyboard tooltip closes on Escape with nothing selected
                expect(page.locator("#tip")).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator("#tip")).to_be_hidden()
                mock.cluster["objects"] = False
                expect(page.locator("#cluster-status")).to_contain_text("could not be read", timeout=8000)
                expect(page.locator("#btn-apply")).to_be_disabled()
                expect(page.locator("#btn-spike")).to_be_disabled()

            scenario("unknown-usage-is-not-zero", unknown_usage_is_not_zero)

            def finished_job_pods_dont_blank_usage(page, mock):
                # Review F09: a completed load-generator pod (no samples, ever) made the whole total unknown.
                mock.cluster["nodes"][0]["pods"].append({"name": "kubesage-spike-test-x1", "app": "kubesage-spike-test",
                                                         "phase": "Succeeded", "ready": False, "restarts": 0,
                                                         "cpu_request_m": 100, "mem_request_mi": 64, "mem_limit_mi": 256,
                                                         "cpu_used_m": None, "mem_used_mi": None})
                ready(page, url)
                expect(page.locator("#inspector .big")).not_to_have_text("—")
                expect(page.locator("#inspector")).not_to_contain_text("pods sampled")

            scenario("finished-job-pods-dont-blank-usage", finished_job_pods_dont_blank_usage)

            def demo_says_what_it_measured(page, mock):
                # Review F21: a finished traffic Job read as success whatever its requests did; a team run's time was
                # called "Alert to fix"; an unknown plan passed as the "before" state; a bigger pool showed $0 saved.
                mock.cluster["traffic"] = traffic_fixture(errors=20)
                page.goto(url + "#demo")
                scales = page.locator("#demo-acts .ds").nth(4)
                expect(scales.locator(".ds-st")).to_contain_text("20 of 520 requests failed")
                expect(scales).to_have_attribute("data-state", "warn")
                figs = page.locator("#demo-figs")
                expect(figs).to_contain_text("Team run")
                expect(figs).to_contain_text("start to diagnosis and commit")
                expect(figs).to_contain_text("not applied yet")
                expect(figs).not_to_contain_text("Alert to fix")
                mock.cluster["traffic"] = traffic_fixture(logs=False)
                mock.cluster["plan"].update(state="unknown")
                expect(scales.locator(".ds-st")).to_contain_text("request results are unknown", timeout=8000)
                expect(page.locator("#demo-preflight")).to_contain_text("FinOps plan state unknown")
                expect(page.locator("#demo-preflight")).not_to_contain_text("oversized")
                mock.report["node_pool"]["after_monthly_usd"] = mock.report["node_pool"]["before_monthly_usd"] + 50
                page.reload()
                expect(figs).to_contain_text("Pool cost change")
                expect(figs).to_contain_text("+$50.00")

            scenario("demo-says-what-it-measured", demo_says_what_it_measured)

            def panels_leave_room_for_the_view(page, mock):
                # Review F23: both panels at their widest left the view 61 px at 1024 and 84 px at 1400.
                room = lambda: page.evaluate("""() => { const b = getComputedStyle(document.body);
                    return innerWidth - parseFloat(b.paddingLeft) - parseFloat(b.paddingRight); }""")
                for width, floats in ((1024, True), (1400, False)):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(url + "#agents")
                    expect(page.locator("#team-flow .agent")).to_have_count(6)
                    if page.locator("#chat").is_hidden():
                        page.locator("#btn-chat").click()
                    if page.locator("#txside").is_hidden():
                        page.locator("#btn-side").click()
                    for grip, key in (("#chat-grip", "ArrowRight"), ("#txs-grip", "ArrowLeft")):
                        page.locator(grip).focus()
                        for _ in range(15):
                            page.keyboard.press(key)
                    assert room() >= 560, (width, room())
                    assert page.evaluate("document.body.classList.contains('side-over')") == floats, width
                    if floats:  # over the view: it keeps the room, and the panel is still all on screen
                        side = page.locator("#txside").bounding_box()
                        assert side["x"] >= 0 and side["x"] + side["width"] <= width + 1, side
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            scenario("panels-leave-room-for-the-view", panels_leave_room_for_the_view)

            def responsive(page, mock):
                mock.cluster["traffic"] = traffic_fixture(errors=20)
                ready(page, url)
                page.locator(".plan-summary > summary").click()
                layout = check_layout(page)
                page.evaluate("window.scrollTo(0, 0)")
                theme = page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'")
                page.screenshot(path=str(output / f"ui-{page.viewport_size['width']}-{theme}.png"), full_page=True)
                for selector, name in (("#cluster-card", "cluster"), ("#capacity-card", "capacity"), ("#decision-card", "decisions")):
                    page.locator(selector).screenshot(path=str(output / f"{name}-{page.viewport_size['width']}-{theme}.png"),
                                                     style="header.top, .skip { visibility: hidden !important; }")
                assert not layout["pageOverflow"], layout
                assert not layout["outside"], layout
                wrapped = page.locator("#ledger td:last-child").evaluate_all("""cells => cells.filter(cell => {
                    const range = document.createRange(); range.selectNodeContents(cell);
                    return [...range.getClientRects()].some(rect => rect.height > 0) &&
                      new Set([...range.getClientRects()].filter(r=>r.height>0).map(rect=>Math.round(rect.top))).size > 1;
                }).map(cell => cell.textContent)""")
                assert not wrapped, f"Ledger costs wrap across lines: {wrapped}"
                assert page.locator("#headline").evaluate("el=>getComputedStyle(el).animationName") == "none"
                assert not page.locator("#headline").evaluate("el=>el.classList.contains('sweeping')")

            for width in (390, 768, 1400):
                for theme in ("light", "dark"):
                    scenario(f"responsive-{width}-{theme}", responsive, width=width, theme=theme)
            browser.close()
    finally:
        server.shutdown()
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="/tmp/kubesage-browser")
    parser.add_argument("--executable", help="Existing Chromium executable; otherwise use Playwright's installed browser")
    raise SystemExit(run(parser.parse_args()))
