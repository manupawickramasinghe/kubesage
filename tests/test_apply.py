import json
import subprocess
from pathlib import Path

import yaml

from kubesage import apply
from kubesage.llm.schemas import CronWindow, Recommendation, ResourceSpec, ScalingPlan
from kubesage.recommend.render import render


def _write_run(tmp_path: Path) -> Path:
    rec = Recommendation(workload="demo/report-worker", risk="low", diagnosis="batch",
                         resources=ResourceSpec(cpu_request_m=1900, cpu_limit_m=2400, mem_request_mi=1400, mem_limit_mi=1632),
                         scaling=ScalingPlan(min_replicas=0, max_replicas=1,
                                             cron_windows=[CronWindow(start="50 1 * * *", end="0 3 * * *", desired_replicas=1)]))
    run = tmp_path / "20260101-000000"
    (run / "manifests").mkdir(parents=True)
    for name, text in render(rec, namespace="demo", name="report-worker", container="report-worker", tz="UTC").items():
        (run / "manifests" / name).write_text(text)
    (run / "report.json").write_text("{}")
    return run


def test_patch_has_header_and_plan_orders_patch_before_scaledobject(tmp_path):
    run = _write_run(tmp_path)
    patch = yaml.safe_load((run / "manifests/report-worker.patch.yaml").read_text())
    assert (patch["apiVersion"], patch["kind"], patch["metadata"]) == (
        "apps/v1", "Deployment", {"name": "report-worker", "namespace": "demo"})
    steps = apply.plan(run, kubectl=["kubectl"], dry_run=True)
    assert len(steps) == 2
    assert steps[0].cmd[:6] == ["kubectl", "-n", "demo", "patch", "deployment", "report-worker"]
    assert steps[1].cmd[:4] == ["kubectl", "apply", "-f", str(run / "manifests/report-worker.keda.yaml")]
    assert all(s.cmd[-1] == "--dry-run=server" for s in steps)
    assert apply.latest_run(tmp_path) == run


def test_migration_skipped_without_matching_node(tmp_path):
    from kubesage.recommend.render import migration

    run = _write_run(tmp_path)
    (run / "manifests/report-worker.migrate.yaml").write_text(migration("demo", "report-worker", "kubesage.io/sku", "F4s_v2"))
    names = lambda steps: [(s.file.name, s.skipped is None) for s in steps]
    assert names(apply.plan(run, kubectl=["kubectl"], available_skus={"D8s_v5"})) == [
        ("report-worker.patch.yaml", True), ("report-worker.migrate.yaml", False), ("report-worker.keda.yaml", True)]
    assert names(apply.plan(run, kubectl=["kubectl"], available_skus={"D8s_v5", "F4s_v2"}))[1] == ("report-worker.migrate.yaml", True)


def test_migration_requires_the_whole_planned_pool(tmp_path):
    from kubesage.recommend.render import migration

    run = _write_run(tmp_path)
    (run / "manifests/report-worker.migrate.yaml").write_text(
        migration("demo", "report-worker", "kubesage.io/sku", "F4s_v2"))
    (run / "report.json").write_text(json.dumps({"node_pool": {"choice": {"sku": "F4s_v2", "nodes": 2}}}))
    steps = apply.plan(run, kubectl=["kubectl"], available_node_counts={"F4s_v2": 1})
    assert not steps[0].skipped and not steps[2].skipped  # safe sizing and scaling can still be applied
    assert "1/2 Ready" in steps[1].skipped
    assert not apply.plan(run, kubectl=["kubectl"], available_node_counts={"F4s_v2": 2})[1].skipped
    assert "0/2 Ready" in apply.plan(run, kubectl=["kubectl"], available_node_counts={})[1].skipped


def test_ready_node_counts_exclude_unready_and_cordoned_nodes(monkeypatch):
    def node(sku, *, ready="True", cordoned=False):
        return {"metadata": {"labels": {"kubesage.io/sku": sku}},
                "spec": {"unschedulable": cordoned},
                "status": {"conditions": [{"type": "Ready", "status": ready}]}}

    nodes = [node("F4s_v2"), node("F4s_v2"), node("F4s_v2", ready="False"),
             node("F4s_v2", ready="Unknown"), node("F4s_v2", cordoned=True), node("D8s_v5")]
    monkeypatch.setattr(apply.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, json.dumps({"items": nodes}), ""))
    assert apply.ready_node_counts("kubesage.io/sku", ["kubectl"]) == {"F4s_v2": 2, "D8s_v5": 1}


def test_unreachable_cluster_has_no_ready_capacity(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(apply.subprocess, "run", timeout)
    assert apply.ready_node_counts("kubesage.io/sku", ["kubectl"]) == {}
    assert apply.node_skus("kubesage.io/sku", ["kubectl"]) == set()


def test_failed_patch_prevents_dependent_mutations(tmp_path, monkeypatch):
    steps = apply.plan(_write_run(tmp_path), kubectl=["kubectl"])
    commands = []

    def failure(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "deployment patch rejected")

    monkeypatch.setattr(apply.subprocess, "run", failure)
    result = apply.run(steps)
    assert len(commands) == 1
    assert result[0].returncode == 1 and "rejected" in result[0].output
    assert result[1].returncode is None and "failed" in result[1].skipped


def test_apply_timeout_is_reported_and_stops_the_plan(tmp_path, monkeypatch):
    steps = apply.plan(_write_run(tmp_path), kubectl=["kubectl"])

    def timeout(cmd, **kwargs):
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(apply.subprocess, "run", timeout)
    result = apply.run(steps)
    assert result[0].returncode == 124 and "timed out" in result[0].output
    assert result[1].returncode is None and result[1].skipped


def test_latest_run_ignores_incomplete_and_staging_directories(tmp_path):
    run = _write_run(tmp_path)
    (tmp_path / "20990101-000000/manifests").mkdir(parents=True)
    staging = tmp_path / ".run-unpublished"
    (staging / "manifests").mkdir(parents=True)
    (staging / "report.json").write_text("{}")
    assert apply.latest_run(tmp_path) == run


def test_a_fixed_replica_plan_removes_the_autoscaler_it_replaces(tmp_path, monkeypatch):
    # Review F07: a fixed replica count left the previous run's ScaledObject in place, so its HPA kept resizing.
    rec = Recommendation(workload="demo/api", risk="low", diagnosis="steady",
                         scaling=ScalingPlan(min_replicas=2, max_replicas=2))
    run = tmp_path / "20260101-000000"
    (run / "manifests").mkdir(parents=True)
    for name, text in render(rec, namespace="demo", name="api", container="api", tz="UTC").items():
        (run / "manifests" / name).write_text(text)
    (run / "report.json").write_text("{}")
    steps = apply.plan(run, kubectl=["kubectl"])
    assert [s.cmd[3:6] for s in steps] == [["delete", "scaledobjects.keda.sh", "-l"], ["patch", "deployment", "api"]]
    assert "metadata.name=api" in steps[0].cmd and "app.kubernetes.io/managed-by=kubesage" in steps[0].cmd
    # without KEDA there is nothing to remove, and the patch still runs
    outputs = iter([(1, "", "error: the server doesn't have a resource type \"scaledobjects\""), (0, "patched", "")])
    monkeypatch.setattr(apply.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, *next(outputs)))
    done = apply.run(steps)
    assert [s.returncode for s in done] == [0, 0] and "KEDA isn't installed" in done[0].output
    # a run that autoscales keeps its ScaledObject: nothing to remove
    assert all("delete" not in s.cmd for s in apply.plan(_write_run(tmp_path / "b"), kubectl=["kubectl"]))
