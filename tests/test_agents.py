"""The agent team engine, the agents/ folder, the RAM ring, the live test cases and their web endpoints."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from kubesage import lab, ring
from kubesage.agents import edits as E
from kubesage.agents import loader, repo, team
from kubesage.agents.context import Context
from kubesage.agents.schemas import Action, Diagnosis, Edit, Issue
from kubesage.config import load_skus
from kubesage.web import server as web

ROOT = Path(__file__).resolve().parents[1]


# ---- ring ----

def test_ring_wraps_and_keeps_the_newest_records(tmp_path):
    r = ring.Ring(tmp_path / "ring", segsize=4096, size_mb=1)
    for i in range(40000):  # ~1.6 MB of records into a 1 MiB ring
        r.write(f"s{i % 2}", f"line {i}")
    st = r.stat()
    assert st["wrapped"] and st["last_seq"] == 40000 and st["records"] < 40000
    recs, last = r.read(0, limit=10)
    assert last == 40000 and recs[0].seq > 1  # the oldest were overwritten
    recs, _ = r.read(39995)
    assert [x.text for x in recs] == [f"line {i}" for i in range(39995, 40000)]
    tail, _ = r.tail(3, streams=["s1"])
    assert [x.text for x in tail] == ["line 39995", "line 39997", "line 39999"]


def test_ring_is_shared_between_openers_and_threads(tmp_path):
    path = tmp_path / "ring"
    writers = [ring.Ring(path, size_mb=1, segsize=8192) for _ in range(3)]  # separate opens, like separate processes
    threads = [threading.Thread(target=lambda w=w, n=n: [w.write(f"w{n}", f"{n}:{i}") for i in range(300)])
               for n, w in enumerate(writers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    recs, last = ring.Ring(path, create=False).read(0, limit=5000)
    assert last == 900 and len(recs) == 900 and len({x.seq for x in recs}) == 900
    with pytest.raises(FileNotFoundError):
        ring.Ring(tmp_path / "missing", create=False)


def test_ring_write_strips_terminal_escapes():
    from kubesage.cli import _clean
    assert _clean("\x1b[32mok\x1b[0m done\r\n") == "ok done"
    assert _clean("progress 10%\rprogress 100%") == "progress 100%"
    assert _clean("\x1b]0;title\x07$ ls") == "$ ls"


# ---- loader ----

def test_the_repo_agents_folder_loads_as_a_team():
    t = loader.load_team(ROOT / "agents", profile="cerebras")
    assert [a.name for a in t.subagents] == ["promql", "resources", "alerts", "security"]
    assert t.thinker.tier.extra_body == {"reasoning_effort": "high"} and t.executor.tier.model == "gpt-oss-120b"
    assert t.subagents[0].tier.model == "qwen-3.8-27b" and all(a.collect for a in t.subagents)
    assert [n for n, _ in t.rules] == ["platform.txt"]  # README.md is documentation, not a rule
    assert set(t.models().tiers) == {a.name for a in t.agents}


def test_a_new_agent_folder_joins_the_team(tmp_path):
    shutil.copytree(ROOT / "agents", tmp_path / "agents")
    new = tmp_path / "agents" / "network"
    shutil.copytree(tmp_path / "agents" / "alerts", new)
    cfg = yaml.safe_load((new / "agent.yaml").read_text())
    (new / "agent.yaml").write_text(yaml.safe_dump({**cfg, "name": "network", "order": 50, "model": "medium"}))
    off = yaml.safe_load((tmp_path / "agents" / "security" / "agent.yaml").read_text())
    (tmp_path / "agents" / "security" / "agent.yaml").write_text(yaml.safe_dump({**off, "enabled": False}))
    (tmp_path / "agents" / "rules" / "team.txt").write_text("- never touch payment-service on Fridays\n")
    t = loader.load_team(tmp_path / "agents", profile="cerebras")
    assert [a.name for a in t.subagents] == ["promql", "resources", "alerts", "network"]
    assert t.subagents[-1].tier.model == "gpt-oss-120b" and "Fridays" in t.rules_text()


def test_an_unknown_model_size_is_an_error(tmp_path):
    shutil.copytree(ROOT / "agents", tmp_path / "agents")
    path = tmp_path / "agents" / "alerts" / "agent.yaml"
    path.write_text(path.read_text().replace("model: small", "model: huge"))
    with pytest.raises(loader.TeamError, match="huge"):
        loader.load_team(tmp_path / "agents", profile="cerebras")


# ---- edits and guardrails ----

DEPLOY = (ROOT / "gitops/apps/ledger-cache/deployment.yaml").read_text()


def test_edits_must_match_once_and_stay_under_gitops(tmp_path):
    e = Edit(path="gitops/apps/x.yaml", find="memory: 512Mi", replace="memory: 1Gi", action="A1")
    assert "memory: 1Gi" in E.apply_edit(DEPLOY, e)
    with pytest.raises(E.EditError, match="0 times"):
        E.apply_edit(DEPLOY, e.model_copy(update={"find": "memory: 9Gi"}))
    with pytest.raises(E.EditError, match="already exists"):
        E.apply_edit(DEPLOY, e.model_copy(update={"find": ""}))
    for bad in ("deploy/k8s/demo/workloads.yaml", "gitops/../kubesage/cli.py", "/etc/passwd"):
        with pytest.raises(E.EditError):
            E.check_path(tmp_path, bad)


def test_a_misindented_find_still_applies_once_reindented():
    e = Edit(path="gitops/x.yaml", action="A1", find="requests: {cpu: 50m, memory: 256Mi}\nlimits: {cpu: 500m, memory: 512Mi}",
             replace="requests: {cpu: 50m, memory: 1Gi}\nlimits: {cpu: 500m, memory: 1Gi}")
    out = E.apply_edit(DEPLOY, e)
    assert "            requests: {cpu: 50m, memory: 1Gi}\n            limits: {cpu: 500m, memory: 1Gi}\n" in out
    assert len(out.splitlines()) == len(DEPLOY.splitlines())
    with pytest.raises(E.EditError):  # ambiguous or absent blocks are still refused
        E.apply_edit(DEPLOY, e.model_copy(update={"find": "nothing like this"}))


def test_validation_catches_duplicate_keys_bad_python_and_unknown_skus():
    skus = {s.name for s in load_skus().skus}
    with pytest.raises(E.EditError, match="duplicate"):
        E.validate_file("a.yaml", "kind: Namespace\nmetadata:\n  labels:\n    a: x\n    a: y\n", skus)
    with pytest.raises(E.EditError, match="compile"):
        E.validate_file("a.py", "def f(:\n", skus)
    with pytest.raises(E.EditError, match="not in config/skus.yaml"):
        E.validate_file("a.tf", 'vm_size = "Standard_Z99"\n', skus)
    E.validate_file("a.tf", 'resource "x" "y" {\n  vm_size = "Standard_E8s_v5"\n}\n', skus)


def test_deterministic_edits_cover_the_structured_actions():
    files = {p: (ROOT / p).read_text() for p in ("gitops/apps/ledger-cache/deployment.yaml", "gitops/infra/nodepools.tf",
                                                 "gitops/apps/namespace.yaml", "gitops/apps/risk-scorer/src/risk_scorer.py")}
    act = lambda **kw: Action(id="A1", issue="I1", summary="s", rationale="r", **kw)
    [res] = E.deterministic(act(type="set_resources", target="deployment/lab/ledger-cache",
                                params={"limits": {"memory": "1Gi"}}), files)
    assert res.replace.strip() == "limits: {cpu: 500m, memory: 1Gi}"
    [sku] = E.deterministic(act(type="change_node_sku", target="nodepool/lab", params={"pool": "lab", "to_sku": "E8s_v5"}), files)
    assert '"Standard_E8s_v5"' in sku.replace and '"kubesage.io/sku"  = "E8s_v5"' in sku.replace and "default_node_pool" not in sku.find
    [ns] = E.deterministic(act(type="security_hardening", target="namespace/lab", params={"pod_security": "baseline"}), files)
    assert "pod-security.kubernetes.io/enforce: baseline" in ns.replace
    assert E.deterministic(act(type="security_hardening", target="namespace/demo"), files) == []  # demo isn't in gitops/
    [code] = E.deterministic(act(type="code_change", target="deployment/lab/risk-scorer",
                                 params={"file": "gitops/apps/risk-scorer/src/risk_scorer.py"}), files)
    assert "ProcessPoolExecutor(max_workers=WORKERS)" in code.replace and "ThreadPool" not in code.replace
    compile(code.replace, "risk_scorer.py", "exec")


INV = {"workloads": [{"name": "ledger-cache", "namespace": "lab", "node_selector": {"kubesage.io/pool": "lab"}}],
       "nodes": [{"name": "laptop", "pool": "lab", "allocatable": {"cpu": "8", "memory": "14749276Ki"}}],
       "node_pools": [{"name": "lab", "sku": "F8s_v2", "node_count": 1}]}


def test_the_thinker_is_held_to_its_evidence_targets_and_todays_nodes():
    tr = team.TeamRun(loader.load_team(ROOT / "agents", profile="cerebras"), "test", "mock")
    issue = lambda id, ev: Issue(id=id, title=id, category="memory", severity="critical", confidence=0.9, root_cause="x",
                                 evidence=ev, targets=["deployment/lab/ledger-cache"])
    act = lambda id, **kw: Action(id=id, issue="I1", summary="s", rationale="r", **kw)
    d = Diagnosis(summary="", issues=[issue("I1", ["promql:a", "made-up"]), issue("I2", ["made-up"])], actions=[
        act("A1", type="set_resources", target="deployment/lab/ledger-cache", params={"limits": {"memory": "1Gi"}}),
        act("A2", type="set_resources", target="deployment/lab/ledger-cache", params={"requests": {"memory": "20Gi"}}),
        act("A3", type="change_node_sku", target="nodepool/lab", params={"to_sku": "Standard_E8s_v5"}),
        act("A4", type="change_node_sku", target="nodepool/lab", params={"to_sku": "M416ms_v2"}),
        act("A5", type="set_resources", target="deployment/lab/not-there"),
        act("A6", type="remove_workload", target="pod/lab/ledger-cache")])
    out = team._check_diagnosis(d, {"promql:a": {}}, INV, load_skus(), tr)
    assert [i.id for i in out.issues] == ["I1"] and out.issues[0].evidence == ["promql:a"]
    assert [a.id for a in out.actions] == ["A1", "A3", "A6"] and out.actions[2].needs_human
    assert out.actions[1].params["from_sku"] == "F8s_v2" and out.actions[1].params["monthly_delta_usd"] == 121.18
    reasons = " | ".join(r["reason"] for r in tr.data["rejected"])
    assert "Pending" in reasons and "M416ms_v2" in reasons and "not in the inventory" in reasons


# ---- a whole team run, mock mode, against a throwaway git origin ----

def _origin(tmp_path) -> str:
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    shutil.copytree(ROOT / "gitops", work / "gitops")
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(work), *args], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(tmp_path / "origin.git")], check=True)
    return str(tmp_path / "origin.git")


def test_a_mock_team_run_diagnoses_and_commits_a_fix(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    shutil.copytree(ROOT / "agents", agents)
    for name in ("promql", "resources", "alerts", "security"):  # canned collectors: no cluster in the test
        (agents / name / "collect.py").write_text("def collect(ctx):\n    pass\n")
    (agents / "promql" / "collect.py").write_text(
        "def collect(ctx):\n"
        "    t = 'deployment/lab/ledger-cache'\n"
        "    ctx.signal('ledger-cache.memory', 'memory_working_set', t, 500, unit='MiB', detail='limit 512 MiB', severity='warning')\n"
        "    ctx.signal('ledger-cache.oom', 'oom_kills_10m', t, 3, unit='kills', severity='critical')\n")
    deployment = yaml.safe_load(DEPLOY)
    nodes = {"items": [{"metadata": {"name": "laptop", "labels": {"kubesage.io/pool": "lab", "kubesage.io/sku": "F8s_v2"}},
                        "status": {"capacity": {"cpu": "8", "memory": "14749276Ki", "pods": "110"},
                                   "allocatable": {"cpu": "8", "memory": "14749276Ki", "pods": "110"}, "nodeInfo": {}}}]}
    monkeypatch.setattr(Context, "kube", lambda self, *a: nodes if a[:2] == ("get", "nodes") else
                        {"items": [deployment]} if "deployments" in a else {"items": []})
    monkeypatch.setattr(E, "kustomize_ok", lambda d: None)
    monkeypatch.setenv("KUBESAGE_RING", str(tmp_path / "ring"))
    monkeypatch.setattr(ring, "_shared", None)
    t = loader.load_team(agents, profile="cerebras")
    s = team.TeamSettings(vm="http://127.0.0.1:9", alertmanager="http://127.0.0.1:9", out_dir=tmp_path / "out", mode="mock",
                          repo_url=_origin(tmp_path), base_branch="main")
    d = team.run(t, s, trigger="manual").snapshot()
    assert d["state"] == "done" and d["mode"] == "mock" and d["agents"]["promql"]["signals"] == 2
    assert [i["category"] for i in d["diagnosis"]["issues"]] == ["memory"]
    assert {a["type"] for a in d["diagnosis"]["actions"]} == {"set_resources", "change_node_sku"}
    ex = d["executor"]
    assert ex["commit"] and ex["branch"].startswith("agents/") and ex["files"] == [
        "gitops/apps/ledger-cache/deployment.yaml", "gitops/infra/nodepools.tf"]
    assert "+            limits: {cpu: 500m, memory: 1024Mi}" in ex["diff"] and '+  vm_size               = "Standard_E8s_v5"' in ex["diff"]
    assert "Opened by the KubeSage **Executor** agent" in ex["body"]
    assert (tmp_path / "out" / d["id"] / "changes.diff").read_text() == ex["diff"]
    saved = team.load_run(tmp_path / "out", d["id"])
    assert saved["executor"]["commit"] == ex["commit"] and team.list_runs(tmp_path / "out")[0]["id"] == d["id"]
    lines = [r.text for r in ring.Ring(tmp_path / "ring", create=False).iter_since(0, streams=["agent/"])]
    assert any("committed" in x for x in lines) and any("I1" in x for x in lines)
    with pytest.raises(ValueError):
        team.load_run(tmp_path / "out", "../../etc")


def test_the_executor_refuses_a_base_branch_without_gitops(tmp_path):
    origin = _origin(tmp_path)
    work = tmp_path / "w2"
    subprocess.run(["git", "clone", "-q", origin, str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "switch", "-q", "--orphan", "empty"], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "e"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "empty"], check=True)
    clone = repo.ensure(tmp_path / "clone", origin)
    with pytest.raises(repo.RepoError, match="no gitops/apps"):
        repo.start_branch(clone, "empty", "agents/x")
    assert repo.github_slug("git@github.com:me/kubesage.git") == "me/kubesage"


# ---- collectors ----

class FakeCtx:
    def __init__(self, access=(), settings=None):
        self.settings = settings or {"known_paths": ["/", "/api/"]}
        self.signals, self.namespaces, self.gitops_dir = [], [], ROOT / "gitops"
        self._access = list(access)

    def signal(self, id, kind, target, value, **kw):
        self.signals.append({"id": id, "kind": kind, "target": target, "value": value, **kw})

    def access_log(self):
        return self._access

    def kube(self, *a):
        return {"items": []}

    def log(self, text):
        pass


def test_the_security_agent_reports_probes_csrf_rejections_and_refused_commands():
    import importlib.util
    import time
    spec = importlib.util.spec_from_file_location("sec", ROOT / "agents/security/collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    now = time.time()
    hit = lambda path, status, method="GET", refused=None: {"at": now, "client": "192.168.1.50", "method": method,
                                                            "path": path, "status": status, "agent": "curl/8", "refused": refused}
    ctx = FakeCtx([hit("/.env", 404), hit("/.git/config", 404), hit("/wp-login.php", 404), hit("/api/nope", 404),
                   hit("/api/apply", 403, "POST"), hit("/api/shell", 200, "POST", "kubectl get secrets -A")])
    mod.collect(ctx)
    kinds = {x["kind"]: x for x in ctx.signals}
    assert kinds["web_unknown_paths"]["value"] == 3 and kinds["web_unknown_paths"]["severity"] == "warning"
    assert kinds["web_csrf_rejected"]["value"] == 1 and kinds["web_shell_refused"]["value"] == 1


# ---- test cases ----

def test_every_test_case_has_its_manifests():
    for tc in lab.CATALOG:
        if tc.gitops:
            k = yaml.safe_load((ROOT / "gitops/apps" / tc.app / "kustomization.yaml").read_text())
            assert k["namespace"] == "lab"
            for f in k["resources"] + [f for g in k.get("configMapGenerator", []) for f in g["files"]]:
                assert (ROOT / "gitops/apps" / tc.app / f).is_file()
            dep = yaml.safe_load((ROOT / "gitops/apps" / tc.app / "deployment.yaml").read_text())
            assert dep["spec"]["template"]["spec"]["nodeSelector"] == {"kubesage.io/pool": "lab"}
        else:
            assert (ROOT / "kubesage/lab" / f"{tc.app}.yaml").is_file()
    for src in (ROOT / "gitops/apps").glob("*/src/*.py"):
        compile(src.read_text(), str(src), "exec")


def test_test_case_status_comes_from_the_lab_pods():
    pod = lambda name, app, phase, restarts=0, deleting=False: {
        "metadata": {"name": name, "labels": {"app": app}, "creationTimestamp": "2026-10-28T10:00:00Z",
                     **({"deletionTimestamp": "x"} if deleting else {})},
        "status": {"phase": phase, "containerStatuses": [{"restartCount": restarts}]}}
    st = lab.status({"items": [pod("a", "ledger-cache", "Running", 3), pod("b", "batch-fanout", "Pending"),
                               pod("c", "batch-fanout", "Running"), pod("d", "risk-scorer", "Running", deleting=True)]})
    assert st["memory"]["restarts"] == 3 and st["pod-count"]["phases"] == {"Pending": 1, "Running": 1}
    assert st["cpu-code"]["stopping"] and "disk-io" not in st


# ---- web ----

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBESAGE_RING", str(tmp_path / "ring"))
    monkeypatch.setattr(ring, "_shared", None)
    monkeypatch.setattr(lab, "lab_pods", lambda: {"items": []})
    srv = web.serve(web.Settings(vm="http://127.0.0.1:9", namespace="demo", days=30, tz="UTC", mode="mock", out_dir=tmp_path,
                                 alertmanager="http://127.0.0.1:9"), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _req(srv, path, body=None, headers=None):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-KubeSage": "1", "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_web_test_case_and_team_endpoints_validate_input(server):
    assert _req(server, "/api/testcase", {"action": "start", "name": "rm -rf"})[0] == 400
    assert _req(server, "/api/testcase", {"action": "explode", "name": "memory"})[0] == 400
    status, body, _ = _req(server, "/api/testcases")
    assert status == 200 and [t["name"] for t in json.loads(body)["catalog"]][:2] == ["memory", "cpu-throttle"]
    status, body, _ = _req(server, "/api/agents")
    d = json.loads(body)
    assert status == 200 and len(d["team"]["agents"]) == 6 and d["prs"] is False
    assert any(r["file"] == "agents/rules/platform.txt" for r in d["team"]["rules"])
    assert _req(server, "/api/agents/pr", {"id": "20260101-000000"})[0] == 403  # PRs are off unless --agent-prs
    assert _req(server, "/api/agents/run", {"trigger": "alert:$(reboot)"})[0] == 400
    assert _req(server, "/api/agents/auto", {"on": "yes"})[0] == 400


def test_web_ring_is_readable_and_saveable_but_never_writable(server, monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-secret-value-123")
    ring.emit("agent/thinker", "● I1 memory")
    ring.emit("tmux/ops", "echo csk-secret-value-123")
    status, body, _ = _req(server, "/api/ring?streams=agent/thinker")
    assert status == 200 and [r["text"] for r in json.loads(body)["records"]] == ["● I1 memory"]
    assert _req(server, "/api/ring?streams=a;b")[0] == 400
    status, body, headers = _req(server, "/api/ring/dump?minutes=5")
    assert status == 200 and "attachment" in headers["Content-Disposition"] and "● I1 memory" in body
    assert "csk-secret-value-123" not in body and "[redacted]" in body
    assert _req(server, "/api/ring", {"stream": "tmux/ops", "text": "rm -rf /"})[0] == 404  # no write endpoint exists
    assert json.loads(_req(server, "/api/ring/stat")[1])["records"] >= 2


def test_web_access_log_feeds_the_security_agent(server):
    _req(server, "/.env")
    _req(server, "/api/apply", {}, headers={"X-KubeSage": "0"})
    _req(server, "/api/shell", {"cmd": "kubectl delete pod x"})
    lines = [r.text for r in ring.shared().iter_since(0, streams=["access"])]
    assert any("/.env -> 404" in x for x in lines) and any("POST /api/apply -> 403" in x for x in lines)
    assert any("refused: kubectl delete pod x" in x for x in lines)
