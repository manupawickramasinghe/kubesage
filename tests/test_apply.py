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
