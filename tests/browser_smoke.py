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

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        body = request.post_data_json if request.method == "POST" else None
        self.requests.append((url.path, body))
        assert request.headers.get("x-kubesage") == "1", "API helper must carry CSRF header"
        if url.path in self.failures:
            status, value = self.failures[url.path]
        elif url.path == "/api/config":
            status, value = 200, {"mode": "mock", "namespace": "demo", "gitops": True}
        elif url.path == "/api/reports":
            status, value = 200, {"runs": [] if self.empty else [{"run": run, "mode": "mock"} for run in (LATEST, OLDER)]}
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
            status, value = 200, {"run": LATEST, "report": self.report}
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
        elif url.path in ("/api/testcase", "/api/agents/run"):
            if body.get("action") == "start":
                self.active[body["name"]] = {"pods": 1, "phases": {"Running": 1}, "restarts": 0, "since": "x", "stopping": False}
            status, value = (200, {"ok": True, "steps": [{"cmd": "kubectl apply -k gitops/apps/x", "ok": True, "output": ""}]}
                             if url.path == "/api/testcase" else {"id": "20261028-120000"})
        elif url.path == "/api/ring":
            status, value = 200, {"records": [] if parse_qs(url.query).get("after") else ring_fixture(), "last": 9}
        elif url.path == "/api/ring/stat":
            status, value = 200, {"used_bytes": 2 ** 20, "capacity_bytes": 500 * 2 ** 20, "records": 9, "wrapped": False, "oldest_ts": 1.79e9}
        else:
            status, value = 500, {"error": f"Unexpected mocked API route: {url.path}"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


UNSAFE = '<img src=x onerror="window.__untrusted_html_executed=1">'


def team_fixture():
    agent = lambda title, role, size, model, **kw: {"title": title, "role": role, "size": size, "label": model, "model": model,
                                                     "state": "done", "ms": 1500, "signals": 5, "findings": 1, "cost_usd": 0.003, **kw}
    agents = {"promql": agent("PromQL", "subagent", "small", "qwen-3.8-27b", summary=UNSAFE),
              "resources": agent("System resources", "subagent", "small", "qwen-3.8-27b"),
              "alerts": agent("Alertmanager", "subagent", "small", "qwen-3.8-27b"),
              "security": agent("Security", "subagent", "small", "qwen-3.8-27b"),
              "thinker": agent("Thinker", "thinker", "large", "gpt-oss-120b"),
              "executor": agent("Executor", "executor", "medium", "gpt-oss-120b")}
    run = {"id": "20261028-115709", "trigger": "alert:ContainerCPUBusy", "mode": "live", "profile": "cerebras", "state": "done",
           "cost_usd": 0.018, "agents": agents, "rejected": [], "signals": {"promql": [{"id": "promql:risk-scorer.cpu_flat",
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
            "run": run, "runs": [], "auto": True, "prs": True, "pending": None}


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
    page.goto(url)
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
                page.goto(url + "#lab")
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
