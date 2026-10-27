"""The Architecture & CLI lab: read-only shell, request traces, the event journal and its endpoints."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from kubesage import journal
from kubesage.config import load_models
from kubesage.llm.client import LLMClient
from kubesage.llm.schemas import TriageReport
from kubesage.pipeline import write_run
from kubesage.web import server as web
from kubesage.web import shell


def api_for(tmp_path):
    return web.Api(web.Settings(vm="http://127.0.0.1:9", namespace="demo", days=30, tz="UTC", mode="mock", out_dir=tmp_path))


@pytest.fixture
def kubectl_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(shell.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, stdout="NAME   READY\napi-1  1/1\n", stderr=""))
    return calls


@pytest.mark.parametrize("line", [
    "kubectl delete pod api-1", "kubectl apply -f x.yaml", "kubectl exec -it api-1 -- sh", "kubectl edit deploy/api",
    "kubectl get secrets -A", "kubectl get serviceaccounts", "kubectl get pods -w", "kubectl logs api-1 -f",
    "kubectl get pods --kubeconfig=/etc/x", "kubectl get pods; rm -rf /", "kubectl get pods -o jsonpath={.items}",
    "kubectl logs api-1 --tail 5000", "curl http://169.254.169.254/", "bash", "rm -rf /", "kubesage apply",
])
def test_shell_refuses_anything_that_is_not_a_read(tmp_path, kubectl_calls, line):
    result = shell.run(line, namespace="demo", vm="http://127.0.0.1:9", out_dir=tmp_path)
    assert result["exit"] == 2 and not kubectl_calls


def test_shell_runs_reads_as_an_argument_list_scoped_to_the_namespace(tmp_path, kubectl_calls):
    kw = dict(namespace="demo", vm="http://127.0.0.1:9", out_dir=tmp_path)
    assert shell.run("kubectl get pods -o wide", **kw)["exit"] == 0
    assert kubectl_calls[-1][-6:] == ["get", "pods", "-o", "wide", "-n", "demo"]
    shell.run("kubectl get so,hpa -A", **kw)
    assert "scaledobjects.keda.sh,hpa" in kubectl_calls[-1] and "-n" not in kubectl_calls[-1]
    shell.run("kubectl logs deploy/checkout-web", **kw)
    assert kubectl_calls[-1][-5:-2] == ["deploy/checkout-web", "--tail", "100"]  # a default cap on log lines
    shell.run('kubectl get pods -l "app=checkout-web"', **kw)
    assert "app=checkout-web" in kubectl_calls[-1]
    assert shell.run("help", **kw)["out"].startswith("KubeSage lab shell")


def test_shell_kubesage_route_reads_the_latest_run(result, tmp_path):
    write_run(result, tmp_path / "20260101-000000")
    out = shell.run("kubesage route", namespace="demo", vm="http://127.0.0.1:9", out_dir=tmp_path)
    assert out["exit"] == 0 and "routed to" in out["out"] and "demo/catalog-api" in out["out"]
    assert "stop (no more tokens)" in out["out"]
    assert shell.run("kubesage plan", namespace="demo", vm="", out_dir=tmp_path)["out"].startswith("run 20260101-000000")


def test_mock_llm_trace_records_the_request_without_the_key(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-should-never-appear")
    client = LLMClient(load_models(), "mock")
    client.structured("triage", "system prompt", "user prompt", TriageReport, lambda: TriageReport(items=[]),
                      stage="triage", workload="*")
    (exchange,) = client.trace
    assert exchange["kind"] == "llm" and exchange["sent"] is False and exchange["stage"] == 2
    request = exchange["request"]
    assert request["url"].endswith("/chat/completions") and request["body"]["messages"][1]["content"] == "user prompt"
    assert request["headers"]["Authorization"].startswith("Bearer $") and "csk-" not in json.dumps(exchange)


def test_trace_endpoint_returns_exchanges_and_the_exact_kubectl_calls(result, tmp_path):
    run = write_run(result, tmp_path / "20260101-000000", trace=[{"kind": "promql", "request": {"method": "GET"}}])
    status, body = api_for(tmp_path).trace(None)
    assert status == 200 and body["run"] == run.name and body["exchanges"][0]["kind"] == "promql"
    cmds = [k["cmd"] for k in body["kubectl"]]
    assert cmds and all(c.startswith("kubectl ") and str(tmp_path) not in c for c in cmds)
    assert any("apply -f manifests/" in c and "keda" in c for c in cmds) and body["kubectl"][0]["manifest"]
    (run / "trace.json").unlink()  # runs from before tracing still open
    assert api_for(tmp_path).trace(run.name)[1]["exchanges"] == []


def test_kubernetes_events_are_journalled_once_with_their_stage(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    stamp = lambda s: (now - timedelta(seconds=s)).isoformat().replace("+00:00", "Z")  # noqa: E731
    events = {"items": [
        {"metadata": {"uid": "a"}, "type": "Normal", "reason": "SuccessfulRescale", "count": 1, "lastTimestamp": stamp(5),
         "message": "New size: 4; reason: cpu resource utilization above target",
         "involvedObject": {"kind": "HorizontalPodAutoscaler", "name": "keda-hpa-checkout-web"}},
        {"metadata": {"uid": "b"}, "type": "Warning", "reason": "BackOff", "count": 3, "lastTimestamp": stamp(10),
         "message": "Back-off restarting", "involvedObject": {"kind": "Pod", "name": "auth-api-1"}},
        {"metadata": {"uid": "old"}, "type": "Normal", "reason": "Pulled", "count": 1, "lastTimestamp": stamp(3600),
         "message": "ancient", "involvedObject": {"kind": "Pod", "name": "x"}},
    ]}
    monkeypatch.setattr(web, "_kubectl_json", lambda *a: events)
    api = api_for(tmp_path)
    _, first = api.events("0")
    ours = [e for e in first["events"] if e["unit"].startswith("k8s/")]
    assert [(e["unit"], e["stage"], e["level"]) for e in ours] == [("k8s/pod", 5, "warning"),
                                                                 ("k8s/horizontalpodautoscaler", 4, "info")]
    assert "New size: 4" in ours[1]["msg"] and "(x3)" in ours[0]["msg"] and not any("ancient" in e["msg"] for e in ours)
    api._events_read = 0
    _, again = api.events(str(first["last"]))
    assert not [e for e in again["events"] if e["unit"].startswith("k8s/")]  # nothing new, nothing repeated


def test_operations_and_shell_commands_are_journalled(tmp_path, kubectl_calls, monkeypatch):
    monkeypatch.setattr(web, "_kubectl_json", lambda *a: None)
    api = api_for(tmp_path)
    start = journal.JOURNAL.since(0)[1]
    api.shell({"cmd": "kubectl get pods"})
    api.backfill({"days": 0})  # rejected, but the operation is still journalled
    msgs = [e["msg"] for e in journal.JOURNAL.since(start)[0]]
    assert any(m.startswith("$ kubectl get pods -> exit 0") for m in msgs)
    assert any(m.startswith("backfill finished") and "HTTP 400" in m for m in msgs)
    assert api.shell({"cmd": 7})[0] == 400


def test_lab_status_reports_an_unreachable_tsdb_and_never_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value-for-test")
    status, body = api_for(tmp_path).lab()
    assert status == 200 and body["vm"]["ok"] is False and set(body["tiers"]) == {"triage", "worker", "strategist"}
    assert set(body["provider"]) == {"base_url", "api_key_env", "profile"}  # the variable's name, not its value
    assert "secret-value-for-test" not in json.dumps(body)
