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
