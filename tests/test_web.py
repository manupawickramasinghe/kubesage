"""Web UI server: page, report API, CSRF header check, and that the page's script parses."""
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

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


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    page = (web.REPO_ROOT / "kubesage/web/static/index.html").read_text()
    script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    (tmp_path / "page.js").write_text(script)
    subprocess.run(["node", "--check", str(tmp_path / "page.js")], check=True)
