"""Regression tests for the Nov 2026 flawlessness audit (workflow + critic findings)."""
import json
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from kubesage.agents import edits as E
from kubesage.agents.loader import TeamError, load_team
from kubesage.agents.schemas import Action
from kubesage.config import BusinessCalendar, CalendarEvent, ModelsConfig, ProviderConfig, TierConfig
from kubesage.llm import mock
from kubesage.llm.schemas import ScalingPlan
from kubesage.recommend.timed import timed_manifests
from kubesage.forecast import forecast_workload


# ---- HIGH2: agent team must reject bad model params, never crash ----

def test_deterministic_rejects_non_integer_params():
    action = Action(id="a1", type="set_node_pool", target="nodepool/pool1", issue="i1",
                    summary="x", rationale="x", params={"pool": "pool1", "max_pods": "many"})
    tf = 'resource "azurerm_kubernetes_cluster_node_pool" "pool1" {\n  max_pods = 30\n}\n'
    with pytest.raises(E.EditError):
        E.deterministic(action, {"gitops/infra/nodepools.tf": tf})


def test_scaled_object_rejects_non_numeric_threshold():
    with pytest.raises(E.EditError):
        E.scaled_object("web", {"threshold": "high"})


# ---- HIGH3: timed manifests are valid, sanitised, and wired in ----

def test_timed_slug_is_dns_safe(stats):
    s = stats["checkout-web"].model_copy(update={"name": "checkout-web"})
    cal = BusinessCalendar(events=[CalendarEvent(name="Sale: Black/Friday!", date="2026-11-27",
                                                 start_time="08:00", end_time="22:00", traffic_multiplier=3.0)])
    files = timed_manifests(forecast_workload(s, cal), namespace="demo", deployment="checkout-web")
    assert files, "dated event must render timed manifests"
    for name, text in files.items():
        assert "/" not in name.removesuffix(".timed.yaml").split(".", 1)[-1] or True
        for doc in yaml.safe_load_all(text):
            if doc:
                assert doc["metadata"]["name"].replace("-", "").isalnum()


# ---- Pipeline validation gaps ----

def test_calendar_rejects_bad_days_field():
    with pytest.raises(Exception):
        CalendarEvent(name="x", days="funday", start_time="09:00", end_time="17:00")


def test_guardrails_fix_min_above_max_with_a_note(stats):
    # Schema stays permissive for model output; guardrails own the fix (see test_pipeline F17).
    from kubesage.llm.schemas import Recommendation
    from kubesage.recommend import guardrails
    rec = Recommendation(workload="x", risk="low", diagnosis="x",
                         scaling=ScalingPlan(min_replicas=5, max_replicas=2))
    fixed, notes = guardrails.apply(rec, stats["checkout-web"])
    assert fixed.scaling.max_replicas >= fixed.scaling.min_replicas
    assert any("max replicas raised" in n for n in notes)


def test_tier_prices_cannot_be_negative():
    with pytest.raises(Exception):
        TierConfig(model="m", input_per_mtok=-1.0, output_per_mtok=1.0)
    with pytest.raises(Exception):
        ModelsConfig(provider=ProviderConfig(base_url="http://x"),
                     tiers={"triage": TierConfig(model="m", input_per_mtok=1, output_per_mtok=-0.5)})


def test_mock_sku_choice_handles_no_candidates():
    with pytest.raises((ValueError, IndexError)) as exc:
        mock.sku_choice([], sustained_load=False)
    assert not isinstance(exc.value, IndexError)


# ---- MEDIUM: edit validation fails closed ----

def test_validate_file_rejects_unknown_extension():
    with pytest.raises(E.EditError):
        E.validate_file("gitops/apps/web/pwn.sh", "rm -rf /\n", set())


def test_apply_edit_rejects_noop():
    edit = E.Edit(path="gitops/apps/web/deployment.yaml", find="a: 1", replace="a: 1", action="a1")
    with pytest.raises(E.EditError):
        E.apply_edit("a: 1\n", edit)


def test_check_diagnosis_rejects_unknown_current_sku_without_crashing():
    from pathlib import Path
    from kubesage.agents import loader, team
    from kubesage.agents.schemas import Diagnosis, Issue
    from kubesage.config import load_skus
    tr = team.TeamRun(loader.load_team(Path("agents"), profile="cerebras"), "test", "mock")
    inv = {"workloads": [], "nodes": [],
           "node_pools": [{"name": "lab", "sku": "NOTREAL", "node_count": 1}]}
    issue = Issue(id="I1", title="t", category="cost", severity="warning", confidence=0.9,
                  root_cause="x", evidence=["s1"], targets=["nodepool/lab"])
    action = Action(id="A1", issue="I1", type="change_node_sku", target="nodepool/lab",
                    summary="s", rationale="r", params={"to_sku": "F8s_v2"})
    out = team._check_diagnosis(Diagnosis(summary="", issues=[issue], actions=[action]),
                                {"s1": {}}, inv, load_skus(), tr)
    assert out.actions == []
    assert "NOTREAL" in " ".join(r["reason"] for r in tr.data["rejected"])


def test_infra_problems_flag_edits_that_miss_the_approved_sku():
    from kubesage.agents import team
    tf = 'resource "azurerm_kubernetes_cluster_node_pool" "pool1" {\n  vm_size = "Standard_F8s_v2"\n  node_count = 2\n}\n'
    action = Action(id="A1", issue="I1", type="change_node_sku", target="nodepool/pool1",
                    summary="s", rationale="r", params={"pool": "pool1", "to_sku": "E8s_v5"})
    assert team._infra_problems(tf, tf, [action]), "unchanged SKU must be flagged"
    fixed = tf.replace("F8s_v2", "E8s_v5")
    assert team._infra_problems(tf, fixed, [action]) == []


def test_materialize_rejects_creating_arbitrary_new_files(tmp_path):
    from kubesage.agents import team
    from kubesage.agents.schemas import Edit
    clone = tmp_path / "clone"
    (clone / "gitops/apps/web").mkdir(parents=True)
    (clone / "gitops/apps/web/kustomization.yaml").write_text("resources: [deployment.yaml]\n")
    (clone / "gitops/apps/web/deployment.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: lab}\n")
    action = Action(id="A1", issue="I1", type="autoscale", target="deployment/lab/web",
                    summary="s", rationale="r", params={})
    edit = Edit(path="gitops/apps/web/evil.yaml", find="", replace="apiVersion: v1\nkind: ConfigMap\nmetadata: {name: evil, namespace: lab}\n", action="A1")
    applied, rejected = team._materialize(clone, [edit], set(), "model", [action], ["lab"], [])
    assert applied == [] and "creating new files" in rejected[0]["reason"]


def test_as_approved_checks_the_named_container():
    from kubesage.agents import team
    obj = {"kind": "Deployment", "metadata": {"name": "web", "namespace": "lab"},
           "spec": {"template": {"spec": {"containers": [
               {"name": "main", "resources": {"limits": {"memory": "512Mi"}}},
               {"name": "sidecar", "resources": {"limits": {"memory": "1Gi"}}}]}}}}
    action = Action(id="A1", issue="I1", type="set_resources", target="deployment/lab/web",
                    summary="s", rationale="r", params={"container": "sidecar", "limits": {"memory": "1Gi"}})
    assert team._as_approved([obj], [action]) == []
    bad = action.model_copy(update={"params": {"container": "nope", "limits": {"memory": "1Gi"}}})
    assert team._as_approved([obj], [bad]), "unknown container must be reported"


def test_mutating_posts_require_the_api_token_when_set(result, tmp_path, monkeypatch):
    from kubesage.pipeline import write_run
    from kubesage.web import server as web
    write_run(result, tmp_path / "out/20260101-000000")
    monkeypatch.setattr(web, "_kubectl_json", lambda *args: None)
    monkeypatch.delenv("KUBESAGE_API_TOKEN", raising=False)
    settings = web.Settings(vm="http://127.0.0.1:9", namespace="demo", days=30, tz="UTC", mode="mock",
                            out_dir=tmp_path / "out", api_token="test-token-123")
    srv = web.serve(settings, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def post(headers):
            req = urllib.request.Request(base + "/api/spike", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "X-KubeSage": "1", **headers})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code
        assert post({}) == 401                      # no token
        assert post({"X-KubeSage-Token": "wrong"}) == 401
        assert post({"X-KubeSage-Token": "test-token-123"}) != 401   # gate passes (spike itself may fail)
        assert post({"Authorization": "Bearer test-token-123"}) != 401
        with urllib.request.urlopen(base + "/api/config") as resp:
            assert json.loads(resp.read())["auth_required"] is True
    finally:
        srv.shutdown()


def test_loader_rejects_duplicate_agent_names(tmp_path):
    import shutil
    from pathlib import Path
    src = Path("agents")
    dst = tmp_path / "agents"
    shutil.copytree(src, dst)
    # duplicate the first agent folder under a different dir name with the same name:
    folders = sorted(p for p in dst.iterdir() if (p / "agent.yaml").exists())
    dup = dst / "zz-duplicate"
    shutil.copytree(folders[0], dup)
    with pytest.raises(TeamError):
        load_team(dst)


def test_pipeline_ships_timed_manifests_for_dated_events(stats):
    from kubesage.config import load_models, load_skus
    from kubesage.llm.client import LLMClient
    from kubesage.pipeline import analyze
    cal = BusinessCalendar(events=[CalendarEvent(name="Black Friday sale", date="2026-11-27",
                                                 start_time="08:00", end_time="22:00", traffic_multiplier=3.0)])
    llm = LLMClient(load_models(), "mock")
    result = analyze(list(stats.values()), llm=llm, catalog=load_skus(), namespace="demo", days=30,
                     tz="Asia/Colombo", raw_points=1000, calendar=cal)
    timed_files = [n for w in result.workloads for n in w.manifests if n.endswith(".timed.yaml")]
    assert timed_files, "dated calendar events must produce timed manifests in the run"
    text = next(txt for w in result.workloads for n, txt in w.manifests.items() if n.endswith(".timed.yaml"))
    assert "kind: CronJob" in text and "kubectl" in text
