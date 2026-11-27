"""Cost allocation: the arithmetic on hand-made clusters, the simulated cluster end to end, the queries, the
endpoints and the commands. No database: fetch() is tested against a recording client."""
import json

import numpy as np
import pytest

from costs_fixture import END, TZ, simulated_cluster
from kubesage import cli, costs
from kubesage.config import Budget, CostsConfig, load_costs, load_skus
from kubesage.costs import DAYS_PER_MONTH, Node, PodUsage, RawCosts, Volume, build_report, workload_of
from kubesage.metrics.client import PromError
from kubesage.simulate import fleet

CATALOG = load_skus()
D8 = CATALOG.get("D8s_v5")  # 8 vCPU, 32 GiB, $0.384/h


def flat(days: int, **per_day: float) -> PodUsage:
    """A pod with the same core-hours / GiB-hours every day."""
    values = {"cpu_used_h": 0.0, "cpu_req_h": 0.0, "mem_used_h": 0.0, "mem_req_h": 0.0, **per_day}
    return PodUsage(**{k: np.full(days, v) for k, v in values.items()})


def cluster(days: int = 10, nodes: int = 1) -> RawCosts:
    return RawCosts(days=days, end=END, nodes=[Node(f"node-{i}", np.full(days, 24.0), sku="D8s_v5") for i in range(nodes)])


@pytest.fixture(scope="module")
def simulated():
    return build_report(simulated_cluster(), load_costs(), CATALOG, tz=TZ)


# ---------- the arithmetic ----------

def test_a_pod_is_billed_for_the_larger_of_request_and_use_and_the_rest_is_idle():
    raw = cluster()
    raw.pods[("shop", "web-7d9f8c6b5-abcde")] = flat(10, cpu_req_h=48, cpu_used_h=12, mem_req_h=96, mem_used_h=144)
    r = build_report(raw, CostsConfig(), CATALOG)
    node_day = D8.hourly_usd * 24
    cpu_share = 8 / (8 + 32 / CATALOG.cpu_to_gib_cost_ratio)
    core_h, gib_h = node_day * cpu_share / (8 * 24), node_day * (1 - cpu_share) / (32 * 24)
    line = r.by_namespace[0]
    assert line.name == "shop" and line.pods == 1
    assert line.cpu_usd == pytest.approx(48 * core_h * 10, abs=0.01)      # the request: it is above the use
    assert line.ram_usd == pytest.approx(144 * gib_h * 10, abs=0.01)      # the use: it is above the request
    assert line.monthly_usd == pytest.approx((48 * core_h + 144 * gib_h) * DAYS_PER_MONTH, abs=0.01)
    assert line.cpu_request_cores == 2 and line.cpu_used_cores == 0.5 and line.mem_used_gib == 6
    assert line.efficiency == pytest.approx((12 * core_h + 144 * gib_h) / (48 * core_h + 144 * gib_h), abs=0.001)
    c = r.cluster
    assert c.nodes_monthly_usd == pytest.approx(node_day * DAYS_PER_MONTH, abs=0.01)
    assert c.idle_monthly_usd == pytest.approx(c.nodes_monthly_usd - line.monthly_usd, abs=0.02)
    assert c.core_hour_usd == pytest.approx(core_h, abs=1e-5) and c.gib_hour_usd == pytest.approx(gib_h, abs=1e-5)
    assert line.monthly_loaded_usd == pytest.approx(c.monthly_usd, abs=0.02) and line.share_pct == 100


def test_shared_namespaces_and_idle_are_spread_so_the_loaded_column_adds_up_to_the_bill(simulated):
    r = simulated
    for rows in (r.by_namespace, r.by_workload, r.by_team):
        assert sum(l.monthly_loaded_usd for l in rows if not l.shared) == pytest.approx(r.cluster.monthly_usd, abs=0.5)
        assert sum(l.share_pct for l in rows if not l.shared) == pytest.approx(100, abs=1)
        assert sum(l.monthly_usd for l in rows) == pytest.approx(
            r.cluster.allocated_monthly_usd + r.cluster.storage_monthly_usd, abs=0.5)
    assert {l.name for l in r.by_namespace if l.shared} == {"kube-system", "monitoring"}
    assert costs.SHARED in {l.name for l in r.by_team}


def test_days_without_data_are_left_out_of_the_run_rate():
    raw = cluster(days=10)
    raw.nodes[0].hours[:6] = 0  # the cluster is four days old
    usage = flat(10, cpu_req_h=24, mem_req_h=24)
    usage.cpu_req_h[:6] = 0
    usage.mem_req_h[:6] = 0
    raw.pods[("shop", "web-0")] = usage
    r = build_report(raw, CostsConfig(), CATALOG)
    assert r.observed_days == 4
    assert r.cluster.nodes_monthly_usd == pytest.approx(D8.hourly_usd * 24 * DAYS_PER_MONTH, abs=0.01)
    assert r.by_namespace[0].cpu_request_cores == 1  # averaged over the days seen, not the empty ones
    assert any("4 of the 10 days" in n for n in r.notes)


def test_requests_above_capacity_are_reported_not_hidden():
    raw = cluster()
    raw.pods[("shop", "big-0")] = flat(10, cpu_req_h=24 * 12, mem_req_h=24)  # 12 cores requested on an 8-core node
    r = build_report(raw, CostsConfig(), CATALOG)
    assert r.cluster.idle_monthly_usd >= 0
    assert any("billed for more than the nodes cost" in n for n in r.notes)


# ---------- nodes ----------

def test_nodes_are_priced_by_label_then_by_size_then_by_the_configured_pool():
    raw = RawCosts(days=3, end=END)
    hours = np.full(3, 24.0)
    raw.nodes = [Node("a", hours, sku="Standard_F8s_v2"),          # the cloud's instance-type label
                 Node("b", hours, sku=None, cpu=4, mem_gib=15.6),   # no label: its size matches D4s_v5
                 Node("c", hours, sku="m7i.metal", cpu=96, mem_gib=384),  # not in the catalog: priced per core and GiB
                 Node("d", hours)]                                   # nothing known
    raw.pods[("x", "p-0")] = flat(3, cpu_req_h=1)
    r = build_report(raw, CostsConfig(), CATALOG)
    by = {n.name: n for n in r.nodes}
    assert (by["a"].sku, by["a"].priced_by, by["a"].hourly_usd) == ("F8s_v2", "label", 0.338)
    assert (by["b"].sku, by["b"].priced_by, by["b"].hourly_usd) == ("D4s_v5", "capacity", 0.192)
    core_h, gib_h = CATALOG.unit_prices()
    assert by["c"].priced_by == "unit-price" and by["c"].hourly_usd == pytest.approx(96 * core_h + 384 * gib_h, abs=1e-3)
    assert by["d"].priced_by == "assumed" and by["d"].sku == CATALOG.pricing_reference
    assert any("allowlist" in n for n in r.notes) and any("Node d" in n for n in r.notes)

    bare = RawCosts(days=3, end=END, pods={("x", "p-0"): flat(3, cpu_req_h=1)})
    r = build_report(bare, CostsConfig(), CATALOG)
    assert [n.priced_by for n in r.nodes] == ["config"] * CATALOG.current_pool.nodes
    assert r.nodes[0].sku == CATALOG.current_pool.sku and any("No node metrics" in n for n in r.notes)


# ---------- workloads and teams ----------

def test_pods_roll_up_to_their_controller():
    raw = RawCosts(days=1, end=END)
    raw.owners = {("a", "web-5d4f-x"): ("ReplicaSet", "web-5d4f"), ("a", "orphan-rs-pod"): ("ReplicaSet", "api-7c9d8b6f5"),
                  ("a", "db-0"): ("StatefulSet", "db"), ("a", "agent-zz"): ("DaemonSet", "agent"),
                  ("a", "report-29384756-q"): ("Job", "report-29384756"), ("a", "once-x"): ("Job", "migrate")}
    raw.rs_owners = {("a", "web-5d4f"): ("Deployment", "web")}
    raw.job_owners = {("a", "report-29384756"): ("CronJob", "report")}
    assert workload_of("a", "web-5d4f-x", raw) == ("Deployment", "web")
    assert workload_of("a", "orphan-rs-pod", raw) == ("Deployment", "api")      # the ReplicaSet's own owner is gone
    assert workload_of("a", "db-0", raw) == ("StatefulSet", "db")
    assert workload_of("a", "agent-zz", raw) == ("DaemonSet", "agent")
    assert workload_of("a", "report-29384756-q", raw) == ("CronJob", "report")
    assert workload_of("a", "once-x", raw) == ("Job", "migrate")
    # No owner metrics at all: the name is read, and the kind says how sure that is.
    assert workload_of("b", "checkout-web-6b7f9c8d54-x2k9p", raw) == ("Deployment", "checkout-web")
    assert workload_of("b", "orders-db-2", raw) == ("StatefulSet", "orders-db")
    assert workload_of("b", "fluentd-k8s2x", raw) == ("Workload", "fluentd")
    assert workload_of("b", "debug", raw) == ("Pod", "debug")


def test_team_comes_from_the_pod_then_the_namespace_then_the_config():
    raw = cluster()
    for ns, pod in (("a", "one-0"), ("a", "two-0"), ("b", "three-0"), ("c", "four-0"), ("d", "five-0")):
        raw.pods[(ns, pod)] = flat(10, cpu_req_h=24)
    raw.pod_teams = {("a", "one-0"): "payments"}
    raw.namespace_teams = {"a": "storefront", "b": "storefront"}
    r = build_report(raw, CostsConfig(namespace_teams={"b": "ignored", "c": "data"}, shared_namespaces=[]), CATALOG)
    teams = {l.name: l.pods for l in r.by_team}
    assert teams == {"payments": 1, "storefront": 2, "data": 1, costs.UNASSIGNED: 1}


def test_volumes_are_billed_to_the_workload_that_mounts_them():
    raw = cluster()
    raw.pods[("data", "db-0")] = flat(10, cpu_req_h=24)
    raw.owners[("data", "db-0")] = ("StatefulSet", "db")
    raw.volumes = [Volume("data", "data-db-0", np.full(10, 50 * 24.0), pods={"db-0"}),
                   Volume("data", "old-backup", np.full(10, 200 * 24.0))]
    cfg = CostsConfig(storage_usd_per_gib_month=0.10)
    r = build_report(raw, cfg, CATALOG)
    per_gib_day = 0.10 / CATALOG.hours_per_month * 24
    rows = {(l.kind, l.name): l for l in r.by_workload}
    assert rows[("StatefulSet", "db")].storage_usd == pytest.approx(50 * per_gib_day * 10, abs=0.01)
    assert rows[("Volume", "old-backup")].storage_usd == pytest.approx(200 * per_gib_day * 10, abs=0.01)
    assert r.cluster.storage_monthly_usd == pytest.approx(250 * per_gib_day * DAYS_PER_MONTH, abs=0.01)
    unused = [f for f in r.findings if f.kind == "unused-volume"]
    assert [f.target for f in unused] == ["data/old-backup"]
    assert unused[0].monthly_usd == pytest.approx(200 * per_gib_day * DAYS_PER_MONTH, abs=0.01)


# ---------- budgets ----------

def test_budgets_are_graded_against_the_run_rate():
    raw = cluster()
    raw.pods[("shop", "web-0")] = flat(10, cpu_req_h=48, mem_req_h=96)
    monthly = build_report(raw, CostsConfig(), CATALOG).by_namespace[0].monthly_usd
    cfg = CostsConfig(budgets=[Budget(scope="namespace", name="shop", monthly_usd=monthly * 2),
                               Budget(scope="namespace", name="shop", monthly_usd=monthly / 0.9),
                               Budget(scope="team", name=costs.UNASSIGNED, monthly_usd=monthly / 2),
                               Budget(scope="namespace", name="gone", monthly_usd=5),
                               Budget(scope="cluster", monthly_usd=10_000)])
    r = build_report(raw, cfg, CATALOG)
    assert [(b.name, b.status) for b in r.budgets] == [(costs.UNASSIGNED, "over"), ("shop", "at-risk"), ("gone", "unknown"),
                                                       ("shop", "ok"), ("cluster", "ok")]
    assert r.budgets[0].used_pct == pytest.approx(200, abs=0.5) and r.budgets[2].monthly_usd is None
    with pytest.raises(ValueError, match="needs a name"):
        Budget(scope="team", monthly_usd=5)


# ---------- findings ----------

def test_the_simulated_cluster_yields_one_finding_for_each_problem_baked_into_it(simulated):
    found = {(f.kind, f.target) for f in simulated.findings}
    assert {("idle-workload", "data/legacy-export"), ("unused-volume", "data/orders-db-backup-2025"),
            ("no-requests", "platform/node-problem-detector"), ("cost-spike", "staging"),
            ("idle-capacity", "cluster"), ("over-requested", "demo/payment-service")} <= found
    # The nightly batch idles all day but does real work: over-requested, not abandoned.
    assert ("idle-workload", "demo/report-worker") not in found and ("over-requested", "demo/report-worker") in found
    # The control case, and the shared namespaces, are left alone.
    assert not {t for _, t in found} & {"demo/catalog-api", "kube-system/coredns", "monitoring/grafana"}
    spike = next(f for f in simulated.findings if f.kind == "cost-spike")
    assert "load-test" in spike.detail and not spike.counted
    counted = [f for f in simulated.findings if f.counted]
    assert simulated.potential_monthly_usd == pytest.approx(sum(f.monthly_usd for f in counted), abs=0.01)
    assert {f.severity for f in counted} == {"saving"}
    assert [f.severity for f in simulated.findings] == sorted((f.severity for f in simulated.findings),
                                                             key=["saving", "risk", "info"].index)


def test_a_new_or_departed_workload_is_not_called_abandoned():
    raw = cluster(days=10, nodes=1)
    new = flat(10, cpu_req_h=24, mem_req_h=24)
    new.cpu_req_h[:8] = 0
    new.mem_req_h[:8] = 0                      # two days old, hasn't taken traffic yet
    gone = flat(10, cpu_req_h=24, mem_req_h=24)
    gone.cpu_req_h[-2:] = 0
    gone.mem_req_h[-2:] = 0                    # deleted two days ago
    raw.pods[("shop", "new-0")] = new
    raw.pods[("shop", "gone-0")] = gone
    raw.pods[("shop", "idle-0")] = flat(10, cpu_req_h=24, mem_req_h=24, cpu_used_h=0.05)
    r = build_report(raw, CostsConfig(), CATALOG)
    assert [f.target for f in r.findings if f.kind == "idle-workload"] == ["shop/idle"]


def test_small_findings_and_quiet_namespaces_are_not_listed():
    raw = cluster(days=10)
    raw.pods[("shop", "idle-0")] = flat(10, cpu_req_h=24, mem_req_h=24, cpu_used_h=0.05)
    steady = build_report(raw, CostsConfig(min_finding_usd=1000, idle_capacity_warn_pct=100), CATALOG)
    assert steady.findings == [] and steady.potential_monthly_usd == 0
    # One dollar more on the last day is under spike_min_usd_per_day's default share of a $0.75 day... but 5x is not.
    raw.pods[("shop", "idle-0")].cpu_req_h[-1] *= 5
    spiky = build_report(raw, CostsConfig(min_finding_usd=1000, idle_capacity_warn_pct=100), CATALOG)
    assert [f.kind for f in spiky.findings] == ["cost-spike"]


# ---------- the queries ----------

class Recorder:
    """A Prometheus client that answers from a table and remembers what it was asked."""

    def __init__(self, ranges=None, instants=None, fail=()):
        self.ranges, self.instants, self.fail, self.asked = ranges or {}, instants or {}, fail, []

    def _answer(self, table, query):
        self.asked.append(query)
        if any(word in query for word in self.fail):
            raise PromError("boom")
        return next((rows for word, rows in table.items() if word in query), [])

    def query_range(self, query, start, end, step):
        self.window = (start, end, step)
        return self._answer(self.ranges, query)

    def query(self, query, at=None):
        return self._answer(self.instants, query)


def test_fetch_reads_whole_days_and_joins_the_metadata():
    days, end = 3, END + 123  # not on the resolution: it is aligned down
    stamps = [END - 2 * costs.DAY, END - costs.DAY, END]
    pod = {"namespace": "shop", "pod": "web-0"}
    client = Recorder(
        ranges={"container_cpu_usage_seconds_total": [{"metric": pod, "values": [[t, "12"] for t in stamps]}],
                "container_memory_working_set_bytes": [{"metric": pod, "values": [[stamps[2], "48"]]}],
                'resource="cpu"': [{"metric": pod, "values": [[stamps[1], "24"], [stamps[2], "NaN"]]},
                                   {"metric": {"namespace": "", "pod": ""}, "values": [[stamps[0], "9"]]}],
                "kube_node_info": [{"metric": {"node": "n1"}, "values": [[t, "24.3"] for t in stamps]}],
                "kube_persistentvolumeclaim_resource_requests_storage_bytes": [
                    {"metric": {"namespace": "shop", "persistentvolumeclaim": "data"}, "values": [[stamps[2], "240"]]}]},
        instants={"kube_pod_owner": [{"metric": {**pod, "owner_kind": "StatefulSet", "owner_name": "web"}, "value": [end, "1"]},
                                     {"metric": {"namespace": "shop", "pod": "bare", "owner_kind": "<none>", "owner_name": "<none>"}, "value": [end, "1"]}],
                  "kube_pod_labels": [{"metric": {**pod, "label_cost_center": "retail"}, "value": [end, "1"]}],
                  "kube_namespace_labels": [{"metric": {"namespace": "shop", "label_cost_center": "store"}, "value": [end, "1"]}],
                  "kube_node_labels": [{"metric": {"node": "n1", "label_node_kubernetes_io_instance_type": "Standard_D8s_v5"}, "value": [end, "1"]}],
                  "kube_node_status_capacity": [{"metric": {"node": "n1", "resource": "cpu"}, "value": [end, "8"]},
                                                {"metric": {"node": "n1", "resource": "memory"}, "value": [end, str(32 * costs.GIB)]}],
                  "kube_pod_spec_volumes_persistentvolumeclaims_info": [
                      {"metric": {"namespace": "shop", "persistentvolumeclaim": "data", "pod": "web-0"}, "value": [end, "1"]}]},
        fail=("kube_job_owner",))
    raw = costs.fetch(client, days=days, end=end, cfg=CostsConfig(team_label="cost-center"), catalog=CATALOG)
    assert client.window == (END - 2 * costs.DAY, END, costs.DAY)
    usage = raw.pods[("shop", "web-0")]
    assert usage.cpu_used_h.tolist() == [12, 12, 12] and usage.mem_used_h.tolist() == [0, 0, 48]
    assert usage.cpu_req_h.tolist() == [0, 24, 0]              # NaN is no sample; the unlabelled series is dropped
    assert list(raw.pods) == [("shop", "web-0")]
    assert raw.owners == {("shop", "web-0"): ("StatefulSet", "web")}   # "<none>" is not an owner
    assert raw.pod_teams == {("shop", "web-0"): "retail"} and raw.namespace_teams == {"shop": "store"}
    node = raw.nodes[0]
    assert (node.name, node.sku, node.cpu, node.mem_gib) == ("n1", "Standard_D8s_v5", 8, 32)
    assert node.hours.tolist() == [24, 24, 24]                  # never more than a day's hours
    assert raw.volumes[0].pods == {"web-0"} and raw.volumes[0].gib_h.tolist() == [0, 0, 240]
    assert raw.notes == ["Could not read kube_job_owner: boom"]  # optional metadata failing is a note, not an error
    text = "\n".join(client.asked)
    assert "label_cost_center" in text and "label_kubesage_io_sku" in text and "[3d]" in text and "[1d:300s]" in text
    assert 'container!="POD"' in text


def test_fetch_fails_when_usage_cannot_be_read():
    with pytest.raises(PromError):
        costs.fetch(Recorder(fail=("container_cpu_usage_seconds_total",)), days=2, end=END, cfg=CostsConfig(), catalog=CATALOG)


# ---------- the simulated cluster, the exporter, the endpoints, the commands ----------

def test_the_simulated_cluster_is_the_shop_the_docs_describe(simulated):
    r = simulated
    assert [n.sku for n in r.nodes] == ["D8s_v5", "D8s_v5"] and {n.priced_by for n in r.nodes} == {"label"}
    assert r.cluster.nodes_monthly_usd == pytest.approx(2 * 280.32, abs=0.01)
    assert {l.name for l in r.by_namespace} == {"demo", *fleet.namespaces()}
    assert {l.name for l in r.by_team} == {"storefront", "data", "platform", costs.SHARED}
    kinds = {(l.namespace, l.name): l.kind for l in r.by_workload}
    assert kinds[("data", "orders-db")] == "StatefulSet" and kinds[("demo", "checkout-web")] == "Deployment"
    assert kinds[("platform", "node-problem-detector")] == "DaemonSet" and kinds[("data", "orders-db-backup-2025")] == "Volume"
    assert len(r.days) == 30 and r.days == sorted(r.days) and r.observed_days == 30
    assert 30 < r.cluster.idle_pct < 60 and 0.15 < r.cluster.efficiency < 0.4
    assert {(b.name, b.status) for b in r.budgets} == {("staging", "over"), ("cluster", "at-risk"), ("data", "ok"),
                                                       ("storefront", "ok")}
    json.loads(r.model_dump_json())  # every number is finite


def test_every_fleet_series_is_marked_so_a_rerun_replaces_only_them():
    series = fleet.generate_fleet(days=2, end=END, tz=TZ)
    assert all(s.labels.get("kubesage_sim") == "fleet" for s in series)
    assert fleet.DELETE_MATCH == '{kubesage_sim="fleet"}'
    assert "demo" not in fleet.namespaces()
    late = [s for s in series if s.labels.get("pod", "").startswith("load-test-") and s.labels["__name__"].startswith("container_cpu")]
    assert len(late) == 3 and all(s.timestamps_ms[0] / 1000 >= END - 22 * 3600 for s in late)


def test_prometheus_text_escapes_names_and_skips_what_is_unknown(simulated):
    report = simulated.model_copy(deep=True)
    report.by_namespace[0].name = 'we"ird\\ns\n'
    text = costs.prometheus_text(report)
    assert 'kubesage_monthly_cost_usd{scope="namespace",name="we\\"ird\\\\ns "}' in text
    assert f"kubesage_cluster_monthly_cost_usd {report.cluster.monthly_usd}" in text
    assert 'kubesage_budget_used_ratio{scope="namespace",name="staging"} 1.1' in text
    assert all(line.startswith("#") or line.rsplit(" ", 1)[1].replace(".", "").replace("-", "").isdigit()
               for line in text.strip().splitlines())


@pytest.fixture
def api(tmp_path, monkeypatch, simulated):
    from kubesage.web import server

    calls = []

    def fake(**kw):
        calls.append(kw["days"])
        if kw["days"] == 13:
            raise PromError("connection refused")
        return simulated

    monkeypatch.setattr(costs, "costs_now", fake)
    a = server.Api(server.Settings(vm="http://vm", namespace="demo", days=30, tz=TZ, mode="mock", out_dir=tmp_path,
                                   api_token="test-costs-token"))
    a.calls = calls
    return a


def test_the_costs_endpoint_validates_caches_and_reports_failures(api):
    status, body = api.costs()
    assert status == 200 and body["report"]["cluster"]["monthly_usd"] == pytest.approx(582.24, abs=1)
    assert body["config"] == {"team_label": "team", "shared_namespaces": ["kube-system", "monitoring"], "budgets": 4}
    assert api.costs("30")[0] == 200 and api.calls == [30]           # the second read comes from the cache
    assert api.costs("7")[0] == 200 and api.calls == [30, 7]
    for bad in ("0", "91", "abc", "1.5", ""):
        assert api.costs(bad) == (400, {"error": "days must be a whole number from 1 to 90"})
    assert api.costs("13") == (502, {"error": "metrics query failed: connection refused"})
    assert api.metrics_text().startswith("# HELP kubesage_cluster_monthly_cost_usd")


def test_metrics_and_costs_are_served_over_http(api):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from kubesage.web import server

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        auth = {"Authorization": "Bearer test-costs-token"}
        with urllib.request.urlopen(urllib.request.Request(f"{base}/metrics", headers=auth)) as res:
            assert res.headers["Content-Type"].startswith("text/plain") and b"kubesage_budget_used_ratio" in res.read()
        with urllib.request.urlopen(urllib.request.Request(f"{base}/api/costs?days=14", headers=auth)) as res:
            assert json.load(res)["report"]["window_days"] == 30  # the fixture's report, whatever was asked
        with urllib.request.urlopen(f"{base}/js/costs.js") as res:
            assert b"/api/costs" in res.read()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_costs_commands(monkeypatch, capsys, simulated):
    monkeypatch.setattr(costs, "costs_now", lambda **kw: simulated)
    cli.main(["costs", "--json"])
    assert json.loads(capsys.readouterr().out)["cluster"]["monthly_usd"] == simulated.cluster.monthly_usd
    cli.main(["costs", "--prometheus"])
    assert "kubesage_monthly_cost_usd" in capsys.readouterr().out
    for by in ("namespace", "workload", "team"):
        cli.main(["costs", "--by", by, "--top", "3"])
    out = capsys.readouterr().out
    assert "By team" in out and "Findings" in out and "more; --top" in out
    with pytest.raises(SystemExit, match="1 budget.s. over: namespace staging"):
        cli.main(["budgets"])
    assert "over" in capsys.readouterr().out

    within = simulated.model_copy(update={"budgets": [b for b in simulated.budgets if b.status != "over"]})
    monkeypatch.setattr(costs, "costs_now", lambda **kw: within)
    cli.main(["budgets", "--json"])
    assert {b["status"] for b in json.loads(capsys.readouterr().out)} == {"at-risk", "ok"}

    monkeypatch.setattr(costs, "costs_now", lambda **kw: simulated.model_copy(update={"budgets": []}))
    with pytest.raises(SystemExit, match="no budgets configured"):
        cli.main(["budgets"])

    def down(**kw):
        raise PromError("connection refused")

    monkeypatch.setattr(costs, "costs_now", down)
    with pytest.raises(SystemExit, match="metrics query failed: connection refused"):
        cli.main(["costs"])


def test_simulate_writes_the_fleet_beside_the_demo_namespace(monkeypatch, capsys):
    from kubesage.simulate import backfill

    sent = []
    monkeypatch.setattr(backfill, "import_to_vm", lambda vm, series, **kw: sent.append((len(series), kw)) or 1)
    cli.main(["simulate", "--days", "2"])
    assert [kw.get("delete_match") for _, kw in sent] == [None, fleet.DELETE_MATCH]
    assert "kubesage costs" in capsys.readouterr().out
    sent.clear()
    cli.main(["simulate", "--days", "2", "--no-fleet"])
    assert len(sent) == 1


def test_costs_config_defaults_and_validation(tmp_path, monkeypatch):
    from kubesage import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    assert config.load_costs() == CostsConfig() and CostsConfig().shared_namespaces == ["kube-system"]
    (tmp_path / "costs.yaml").write_text("budgets: [{scope: galaxy, monthly_usd: 5}]\n")
    with pytest.raises(ValueError):
        config.load_costs()
    (tmp_path / "costs.yaml").write_text("")
    assert config.load_costs() == CostsConfig()


def test_the_live_scrape_keeps_every_series_the_cost_queries_read():
    """deploy/k8s/monitoring drops every series it isn't told to keep. A metric the cost queries read and the
    scrape drops would silently turn into 'no owners' or 'no nodes' on a real cluster."""
    import re
    from pathlib import Path

    import yaml

    docs = [d for d in yaml.safe_load_all((Path(__file__).parents[1] / "deploy/k8s/monitoring/monitoring.yaml").read_text()) if d]
    scrape = yaml.safe_load(next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "vmagent-scrape")["data"]["scrape.yml"])
    kept = {job["job_name"]: re.compile(job["metric_relabel_configs"][0]["regex"]) for job in scrape["scrape_configs"]
            if job.get("metric_relabel_configs")}
    client = Recorder()
    costs.fetch(client, days=1, end=END, cfg=CostsConfig(), catalog=CATALOG)
    read = set(re.findall(r"\b(container_[a-z_]+|kube_[a-z_]+)\b", "\n".join(client.asked)))
    assert len(read) >= 12
    for metric in read:
        job = "kubelet" if metric.startswith("container_") else "kube-state-metrics"
        assert kept[job].fullmatch(metric), f"{metric} is read by kubesage costs but dropped by the {job} scrape"
    ksm = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "kube-state-metrics")
    args = " ".join(ksm["spec"]["template"]["spec"]["containers"][0]["args"])
    for resource in ("namespaces", "replicasets", "statefulsets", "daemonsets", "jobs", "persistentvolumeclaims"):
        assert resource in args.split("--resources=")[1].split()[0].split(",")
    assert "pods=[team]" in args and "namespaces=[team]" in args and CATALOG.node_label in args
    rules = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "kubesage-kube-state-metrics")["rules"]
    allowed = {r for rule in rules for r in rule["resources"]}
    assert {"namespaces", "persistentvolumeclaims", "replicasets", "statefulsets", "daemonsets", "jobs", "cronjobs"} <= allowed
    assert all(rule["verbs"] == ["list", "watch"] for rule in rules)  # still read-only
