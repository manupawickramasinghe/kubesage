"""Web UI server: page, report API, CSRF header check, and that the page's script parses."""
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from kubesage.pipeline import write_run
from kubesage.web import server as web


@pytest.fixture
def base_url(result, tmp_path, monkeypatch):
    write_run(result, tmp_path / "out/20260101-000000")
    monkeypatch.setattr(web, "_kubectl_json", lambda *args: None)  # no cluster in tests
    settings = web.Settings(vm="http://127.0.0.1:9", namespace="demo", days=30, tz="UTC", mode="mock",
                            out_dir=tmp_path / "out")
    srv = web.serve(settings, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read()


def post(url, body, header=True):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-KubeSage": "1"} if header else {})})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_serves_page_and_report(base_url, result):
    status, page = get(base_url + "/")
    assert status == 200 and b"AI Control Center" in page
    _, body = get(base_url + "/api/report")
    report = json.loads(body)
    assert report["run"] == "20260101-000000"
    assert len(report["report"]["workloads"]) == len(result.workloads)
    assert json.loads(get(base_url + "/api/cluster")[1]) == {"available": False}
    assert json.loads(get(base_url + "/api/config")[1])["mode"] == "mock"
    assert get(base_url + "/fonts/inter-latin-var.woff2")[1][:4] == b"wOF2"  # vendored, no CDN
    with pytest.raises(urllib.error.HTTPError):
        get(base_url + "/fonts/../server.py")


def test_quantities():
    assert [web.cpu_m(q) for q in ("250m", "2", "1.5", "60429740n")] == [250, 2000, 1500, pytest.approx(60.43, abs=0.01)]
    assert [web.mem_mi(q) for q in ("512Mi", "4Gi", "13824Ki", str(2 ** 20))] == [512, 4096, 13.5, 1]


def test_cluster_reports_real_nodes_requests_and_usage(tmp_path, monkeypatch):
    ready = [{"type": "Ready", "status": "True"}]
    node = {"metadata": {"name": "laptop", "labels": {"kubesage.io/sku": "F4s_v2"},
                         "annotations": {"kubesage.io/cpu": "AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx"}},
            "status": {"conditions": ready, "capacity": {"cpu": "8", "memory": "14336Mi"},
                       "allocatable": {"cpu": "8", "memory": "14000Mi"}, "nodeInfo": {"osImage": "Ubuntu 26.04.1 LTS"},
                       "addresses": [{"type": "InternalIP", "address": "192.168.1.92"}]}}
    pod = {"kind": "Pod", "metadata": {"name": "api-1", "labels": {"app": "api"}},
           "spec": {"nodeName": "laptop", "containers": [
               {"resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}}}]},
           "status": {"phase": "Running", "conditions": ready, "containerStatuses": [{"restartCount": 2}]}}
    pending = {"kind": "Pod", "metadata": {"name": "api-2", "labels": {"app": "api"}}, "spec": {"containers": [{}]},
               "status": {"phase": "Pending"}}
    monkeypatch.setattr(web, "_kubectl_json", lambda *a: {"items": [node] if a == ("get", "nodes") else [pod, pending]})
    monkeypatch.setattr(web, "_kubectl_raw", lambda path: {"items": [
        {"metadata": {"name": "laptop"}, "usage": {"cpu": "1250m", "memory": "2097152Ki"}}]} if path.endswith("/nodes") else {"items": [
        {"metadata": {"name": "api-1"}, "containers": [{"usage": {"cpu": "60429740n", "memory": "13824Ki"}}]}]})
    settings = web.Settings(vm="", namespace="demo", days=30, tz="UTC", mode="mock", out_dir=tmp_path)
    _, c = web.Api(settings).cluster()
    agent, unscheduled = c["nodes"]
    assert c["metrics"] and agent["cpu_cores"] == 8 and agent["mem_mi"] == 14336 and agent["alloc_cpu_m"] == 8000
    assert (agent["cpu_used_m"], agent["mem_used_mi"]) == (1250, 2048) and agent["os"] == "Ubuntu 26.04.1 LTS"
    assert agent["cpu_model"].startswith("AMD Ryzen 5 3500U") and agent["internal_ip"] == "192.168.1.92"
    assert (agent["pool"], agent["pool_monthly_usd"]) == ("F4s_v2", 123.37)  # the cost model, kept apart from the hardware
    assert agent["pods"][0] == {"name": "api-1", "app": "api", "phase": "Running", "ready": True, "cpu_request_m": 250,
                                     "mem_request_mi": 512, "mem_limit_mi": 1024, "cpu_used_m": 60.4, "mem_used_mi": 13.5,
                                     "restarts": 2}
    assert unscheduled["role"] == "pending" and unscheduled["pods"][0]["cpu_request_m"] is None


def test_post_requires_header(base_url):
    assert post(base_url + "/api/reset", {}, header=False)[0] == 403
    status, body = post(base_url + "/api/gitops", {})
    assert status == 400 and "gitops-repo" in body["error"]  # header accepted, then rejected for config


def test_analyze_reports_metrics_errors(base_url):
    status, body = post(base_url + "/api/analyze", {})
    assert status == 502 and "metrics query failed" in body["error"]


def test_provider_failure_never_echoes_api_credentials(base_url, monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-sensitive-credential")

    def fail(**kwargs):
        raise web.AnalysisError("Provider failed: test-sensitive-credential")

    monkeypatch.setattr(web, "analyze_now", fail)
    status, body = post(base_url + "/api/analyze", {})
    assert status == 502 and body["error"] == "Provider failed: [redacted]"


def test_reset_never_changes_demo_when_serving_another_namespace(tmp_path, monkeypatch):
    api = api_for(tmp_path)
    api.s.namespace = "production"
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **kw: pytest.fail("must not run reset script"))
    status, body = api.reset()
    assert status == 400 and "demo namespace" in body["error"]


@pytest.mark.parametrize("body", [None, [], "reset", 1, True])
def test_post_rejects_non_object_json(base_url, body):
    status, result = post(base_url + "/api/reset", body)
    assert status == 400 and "object" in result["error"]


@pytest.mark.parametrize("body", [{"dry_run": "false"}, {"run": "../outside"}, {"run": 123}])
def test_apply_rejects_invalid_options(base_url, body):
    assert post(base_url + "/api/apply", body)[0] == 400


@pytest.mark.parametrize("body", [{"rps": float("nan")}, {"rps": 201}, {"seconds": 9}, {"seconds": 10.5},
                                  {"work_ms": True}, {"target": "http://example.com"}, {"target": "api.other-ns"}])
def test_spike_rejects_invalid_options(base_url, body):
    status, result = post(base_url + "/api/spike", body)
    assert status == 400 and result["error"]


def test_report_history_and_requested_run(base_url):
    _, body = get(base_url + "/api/reports")
    runs = json.loads(body)["runs"]
    assert runs[0]["run"] == "20260101-000000" and runs[0]["mode"] == "mock"
    assert isinstance(runs[0]["monthly_savings_usd"], (int, float)) and runs[0]["llm_cost_usd"] >= 0
    _, body = get(base_url + "/api/report?run=20260101-000000")
    assert json.loads(body)["run"] == "20260101-000000"
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base_url + "/api/report?run=../outside")
    assert exc.value.code == 400


def api_for(tmp_path):
    return web.Api(web.Settings(vm="", namespace="demo", days=30, tz="UTC", mode="mock", out_dir=tmp_path))


def test_apply_pins_requested_run_and_reports_partial_failure(result, tmp_path, monkeypatch):
    first = write_run(result, tmp_path / "20260101-000000")
    write_run(result, tmp_path / "20260102-000000")
    planned = []
    monkeypatch.setattr(web.apply, "ready_node_counts", lambda *a: {"F8s_v2": 1})

    def plan(run, **kwargs):
        planned.append((run, kwargs))
        return [web.apply.Step(Path("api.patch.yaml"), [], returncode=1, output="API unavailable")]

    monkeypatch.setattr(web.apply, "plan", plan)
    monkeypatch.setattr(web.apply, "run", lambda steps: steps)
    status, body = api_for(tmp_path).apply({"run": first.name, "dry_run": True})
    assert planned == [(first.resolve(), {"dry_run": True, "available_node_counts": {"F8s_v2": 1}})]
    assert status == 502 and not body["ok"] and body["run"] == first.name
    assert body["steps"][0]["output"] == "API unavailable"


def test_mutations_cannot_race(tmp_path):
    api = api_for(tmp_path)
    api._operation.acquire()
    api.operation = {"name": "reset", "started_at": "2026-01-01T00:00:00+00:00"}
    try:
        for call in (api.analyze, lambda: api.apply({}), api.reset, lambda: api.spike({}), lambda: api.gitops({})):
            status, body = call()
            assert status == 409 and "reset" in body["error"]
    finally:
        api._operation.release()


def test_pinned_apply_requires_a_complete_run(tmp_path, monkeypatch):
    (tmp_path / "incomplete/manifests").mkdir(parents=True)
    monkeypatch.setattr(web.apply, "ready_node_counts", lambda *a: pytest.fail("must reject incomplete run first"))
    status, body = api_for(tmp_path).apply({"run": "incomplete"})
    assert status == 404 and "not found" in body["error"]


def test_report_and_write_operations_stay_in_configured_namespace(result, tmp_path, monkeypatch):
    own = write_run(result, tmp_path / "20260101-000000")
    other = write_run(result.model_copy(update={"namespace": "production"}), tmp_path / "20260102-000000")
    api = api_for(tmp_path)
    api.s.gitops_repo, api.s.gitops_base = tmp_path, "../base"
    monkeypatch.setattr(web.apply, "ready_node_counts", lambda *a: pytest.fail("must reject namespace before kubectl"))
    monkeypatch.setattr(web.gitops, "export", lambda *a, **kw: pytest.fail("must reject namespace before GitOps export"))
    assert api.report()[1]["run"] == own.name
    assert [r["run"] for r in api.reports()[1]["runs"]] == [own.name]
    for body in ({"run": other.name}, {}):
        for call in (api.apply, api.gitops):
            status, response = call(body)
            assert status == 400 and "namespace 'production'" in response["error"]
            assert "configured for 'demo'" in response["error"]
    (own / "report.json").unlink()
    assert api.report()[0] == 404 and api.reports()[1]["runs"] == []


def test_failed_reset_includes_useful_output_and_releases_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(web.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, stdout="", stderr="Error: Kubernetes API unavailable\n"))
    api = api_for(tmp_path)
    status, body = api.reset()
    assert status == 502 and body["error"] == "Reset failed: Error: Kubernetes API unavailable"
    assert body["output"] == ["Error: Kubernetes API unavailable"] and api.operation is None


def test_spike_timeout_is_readable_and_releases_operation(tmp_path, monkeypatch):
    seen = []

    def timeout(*args, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(web.subprocess, "run", timeout)
    api = api_for(tmp_path)
    status, body = api.spike({})
    assert seen == [30] and status == 502 and "30 seconds" in body["error"]
    assert api.operation is None
    assert api._operation.acquire(blocking=False)
    api._operation.release()


def test_plan_state_uses_cluster_specs_and_survives_server_restart(tmp_path):
    run = tmp_path / "20260101-000000"
    manifests = run / "manifests"
    manifests.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps({"namespace": "demo", "node_pool": {"choice": {"sku": "F8s_v2", "nodes": 1}}}))
    spec = {"replicas": 1, "template": {"spec": {"nodeSelector": {"kubesage.io/sku": "F8s_v2"}, "containers": [
        {"name": "api", "resources": {"requests": {"cpu": "1000m", "memory": "1024Mi"},
                                          "limits": {"cpu": None, "memory": "2Gi"}}}]}}}
    manifest = {"kind": "Deployment", "metadata": {"name": "api", "namespace": "demo"}, "spec": spec}
    (manifests / "api.patch.yaml").write_text(yaml.safe_dump(manifest))
    node = {"metadata": {"name": "worker", "labels": {"kubesage.io/sku": "F8s_v2"}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    assert api_for(tmp_path)._plan_status([], [node])["state"] == "pending"
    live = json.loads(json.dumps(manifest))
    live["status"] = {"readyReplicas": 1, "updatedReplicas": 1}
    resources = live["spec"]["template"]["spec"]["containers"][0]["resources"]
    resources["requests"] = {"cpu": "1", "memory": "1Gi"}  # canonical API quantities
    del resources["limits"]["cpu"]
    plan = api_for(tmp_path)._plan_status([live], [node])
    assert plan["state"] == "applied" and plan["ready"] and plan["matched"] == plan["total"] == 1
    assert all(c["match"] for c in plan["workloads"]["api"]["checks"])
    live["metadata"]["generation"] = 2
    live["status"]["observedGeneration"] = 1
    assert not api_for(tmp_path)._plan_status([live], [node])["ready"]  # old ready pods are still rolling over
    live["status"]["observedGeneration"] = 2
    live["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "2"
    assert api_for(tmp_path)._plan_status([live], [node])["state"] == "partial"
    assert api_for(tmp_path)._plan_status([], [node])["state"] == "pending"  # reset, no browser session state
    live["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "1"
    assert api_for(tmp_path)._plan_status([live], [])["state"] == "partial"  # target capacity unavailable


def test_cluster_exposes_autoscaler_target_deployment_and_active_traffic(tmp_path, monkeypatch):
    ready = [{"type": "Ready", "status": "True"}]
    node = {"metadata": {"name": "worker"}, "status": {"conditions": ready}}
    deployment = {"kind": "Deployment", "metadata": {"name": "checkout-web"}, "spec": {"replicas": 2,
                  "template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "500m", "memory": "256Mi"}}}]}}},
                  "status": {"readyReplicas": 2, "updatedReplicas": 2}}
    hpa = {"kind": "HorizontalPodAutoscaler", "metadata": {"name": "autoscaler"}, "spec": {
        "scaleTargetRef": {"name": "checkout-web"}, "minReplicas": 1, "maxReplicas": 5,
        "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"averageUtilization": 70}}}]},
        "status": {"currentReplicas": 2, "desiredReplicas": 3, "currentMetrics": [
            {"type": "Resource", "resource": {"name": "cpu", "current": {"averageUtilization": 95}}}]}}
    job = {"kind": "Job", "metadata": {"name": "kubesage-spike-123"}, "spec": {"template": {"spec": {
        "containers": [{"env": [{"name": "SECONDS", "value": "180"}, {"name": "RPS", "value": "80"}]}]}}},
        "status": {"active": 1, "startTime": "2026-01-01T00:00:00Z"}}
    finished = {**job, "status": {"succeeded": 1}}
    monkeypatch.setattr(web, "_kubectl_json", lambda *a: {"items": [node] if a == ("get", "nodes")
                                                         else [deployment, hpa, job, finished]})
    monkeypatch.setattr(web, "_kubectl_raw", lambda *a: None)
    _, body = api_for(tmp_path).cluster()
    assert body["observed_at"] and body["scaling"]["checkout-web"]["cpu_target"] == 70
    assert body["scaling"]["checkout-web"]["cpu"] == 95
    assert body["deployments"]["checkout-web"]["cpu_request_m"] == 500
    assert body["deployments"]["checkout-web"]["ready"] == 2
    assert len(body["active_jobs"]) == 1 and body["active_jobs"][0]["ends_at"] == "2026-01-01T00:03:00+00:00"


def test_missing_or_timed_out_kubectl_is_an_unavailable_cluster(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("kubectl", 8)

    monkeypatch.setattr(web.subprocess, "run", timeout)
    assert api_for(tmp_path).cluster() == (200, {"available": False})
    assert web._kubectl_raw("/metrics") is None


def traffic_job(name="kubesage-spike-123", **status):
    return {"kind": "Job", "metadata": {"name": name, "namespace": "demo", "creationTimestamp": "2026-01-01T00:00:00Z",
            "labels": {"app.kubernetes.io/managed-by": "kubesage"}}, "status": status}


def test_traffic_parses_only_numeric_interval_reports():
    logs = "\n".join(["loadgen: 80 rps x 50 ms CPU -> http://checkout-web:8080 for 180s",
                      "t=  10s ok=700 err=12 p50=300ms p95=7000ms",
                      "t= 110s ok=729 err=35 p50=694ms p95=7262ms",
                      "t= 120s ok=999 err=99 p50=999ms p95=999ms unsafe suffix",
                      "arbitrary text containing a credential", "<script>alert(1)</script>"])
    assert web._traffic_numbers(logs) == {"logs_available": True, "succeeded_requests": 1429,
        "failed_requests": 47, "elapsed": 110, "p50_ms": 694, "p95_ms": 7262}
    assert web._traffic_numbers(None) == {"logs_available": False, "succeeded_requests": None,
        "failed_requests": None, "elapsed": None, "p50_ms": None, "p95_ms": None}


@pytest.mark.parametrize("status,state", [({}, "pending"), ({"active": 1}, "running"),
                                         ({"succeeded": 1}, "completed"), ({"failed": 1}, "failed"),
                                         ({"conditions": [{"type": "Failed", "status": "True"}]}, "failed")])
def test_traffic_includes_latest_managed_job_even_after_completion(tmp_path, monkeypatch, status, state):
    job = traffic_job(**status)
    older = traffic_job("kubesage-spike-122", active=1)
    older["metadata"]["creationTimestamp"] = "2025-12-31T23:59:00Z"
    unrelated = traffic_job("kubesage-spike-999", active=1)
    unrelated["metadata"]["labels"] = {}
    foreign = traffic_job("kubesage-spike-998", active=1)
    foreign["metadata"]["namespace"] = "other"
    calls = []

    def logs(namespace, name):
        calls.append((namespace, name))
        return "t= 110s ok=729 err=35 p50=694ms p95=7262ms\n"

    monkeypatch.setattr(web, "_kubectl_logs", logs)
    body = api_for(tmp_path)._traffic([older, job, unrelated, foreign])
    assert calls == [("demo", job["metadata"]["name"])]
    assert body["job"] == job["metadata"]["name"] and body["status"] == state
    assert body["failed_requests"] == 35 and body["p95_ms"] == 7262
    assert "logs" not in body  # only numeric measurements leave the server


def test_traffic_unavailable_logs_are_cached_and_job_completion_refreshes_them(tmp_path, monkeypatch):
    calls, now = [], [100]
    monkeypatch.setattr(web.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(web, "_kubectl_logs", lambda *args: calls.append(args))
    api, job = api_for(tmp_path), traffic_job(active=1)
    assert not api._traffic([job])["logs_available"]
    now[0] = 105
    assert api._traffic([job])["failed_requests"] is None and len(calls) == 1
    now[0] = 111
    api._traffic([job])
    assert len(calls) == 2
    job["status"] = {"succeeded": 1}
    assert api._traffic([job])["status"] == "completed" and len(calls) == 3
    assert api._traffic([]) is None  # reset removes the previously displayed result


def test_traffic_log_read_is_bounded_and_handles_kubectl_failure(monkeypatch):
    seen = []

    def timeout(cmd, **kwargs):
        seen.append((cmd, kwargs))
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(web.subprocess, "run", timeout)
    assert web._kubectl_logs("demo", "kubesage-spike-123") is None
    cmd, options = seen[0]
    assert cmd[-4:] == ["demo", "logs", "job/kubesage-spike-123", "--tail=100"]
    assert options["timeout"] == 8


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    page = (web.REPO_ROOT / "kubesage/web/static/index.html").read_text()
    script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    (tmp_path / "page.js").write_text(script)
    subprocess.run(["node", "--check", str(tmp_path / "page.js")], check=True)
