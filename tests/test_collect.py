"""Right-sizing on a cluster shaped like a real one: workloads found through their owners, the main container
picked out of a pod, StatefulSets, several namespaces in one run, and a node pool that counts the pods the
analysis leaves alone. Everything runs against simulated series, without a database."""
import json

import numpy as np
import pytest
import yaml

from kubesage import apply, gitops, service
from kubesage.config import load_skus
from kubesage.llm.schemas import CronWindow, Recommendation, ResourceSpec, ScalingPlan
from kubesage.metrics import cluster
from kubesage.metrics.collect import collect
from kubesage.metrics.stats import WorkloadStats
from kubesage.recommend import render
from kubesage.simulate import fleet
from kubesage.simulate.backfill import Series
from prom_fixture import END, TZ, SeriesProm, cluster_series

CATALOG = load_skus()
DAYS = 3
OWNER_SERIES = ("kube_pod_owner", "kube_replicaset_owner", "kube_job_owner")


@pytest.fixture(scope="module")
def series():
    return cluster_series(DAYS)


@pytest.fixture(scope="module")
def prom(series):
    return SeriesProm(series)


def by_name(client, namespace):
    stats, points = collect(client, namespace=namespace, days=DAYS, end=END, tz=TZ)
    assert points > 0
    return {s.name: s for s in stats}


# ---------- which workload, which container ----------

def test_the_main_container_is_found_and_the_sidecar_is_left_alone(prom):
    staging = by_name(prom, "staging")
    web = staging["checkout-web"]
    assert (web.kind, web.container, web.sidecars) == ("Deployment", "app", ["envoy"])
    assert (web.cpu_request_cores, web.mem_request_mib) == (0.25, 256)  # the app's request, not the app's plus envoy's
    assert web.cpu_pod_p95 < 0.1 and web.mem_pod_p99_mib > 100          # ...and the app's usage
    plain = staging["catalog-api"]
    assert (plain.container, plain.sidecars) == (None, [])  # named like its workload: nothing to say
    assert staging["load-test"].replicas == 3 and staging["load-test"].cpu_request_cores == 1.0


def test_statefulsets_are_right_sized_and_daemonsets_are_not(prom):
    data, platform = by_name(prom, "data"), by_name(prom, "platform")
    assert data["orders-db"].kind == "StatefulSet" and data["orders-db"].replicas == 1
    assert data["redis-cache"].kind == "Deployment"
    assert set(platform) == {"cert-manager", "ingress-nginx-controller"}  # node-problem-detector is a DaemonSet
    assert platform["ingress-nginx-controller"].replicas == 2


def test_the_demo_namespace_reads_as_it_always_did(prom):
    demo = by_name(prom, "demo")
    assert set(demo) == {"payment-service", "report-worker", "auth-api", "checkout-web", "catalog-api"}
    web = demo["checkout-web"]
    assert (web.kind, web.container, web.sidecars, web.replicas) == ("Deployment", None, [], 3)
    assert web.cpu_request_cores == 0.5 and web.cpu_limit_cores == 1.0 and web.mem_limit_mib == 768


def test_without_owner_metrics_the_pod_names_are_read(series):
    bare = SeriesProm(series, drop=OWNER_SERIES)
    data, platform = by_name(bare, "data"), by_name(bare, "platform")
    assert data["orders-db"].kind == "StatefulSet" and data["legacy-export"].kind == "Deployment"
    # A DaemonSet pod's name says only "something with a hash": not enough to resize it.
    assert set(platform) == {"cert-manager", "ingress-nginx-controller"}
    # Replica counts fall back to the pods alive at the end when the controllers' own series are missing too.
    counted = by_name(SeriesProm(series, drop=(*OWNER_SERIES, "kube_deployment_spec_replicas")), "staging")
    assert counted["load-test"].replicas == 3 and counted["catalog-api"].replicas == 1


def test_a_request_that_was_just_lowered_is_not_hidden_by_the_old_pods():
    ts = np.arange(END - DAYS * 86400, END + 1, 300) * 1000
    half = len(ts) // 2
    out = []
    for pod, window, request in (("api-7d9f8c6b5-bbbbb", slice(0, half), 2.0), ("api-5c4d2b2z9-ccccc", slice(half, None), 0.5)):
        labels = {"namespace": "shop", "pod": pod, "container": "api"}
        stamps = ts[window]
        out += [Series({"__name__": "container_cpu_usage_seconds_total", **labels}, stamps, np.cumsum(np.full(len(stamps), 0.1 * 300))),
                Series({"__name__": "container_memory_working_set_bytes", **labels}, stamps, np.full(len(stamps), 200 * 2**20)),
                Series({"__name__": "kube_pod_container_resource_requests", **labels, "resource": "cpu"}, stamps, np.full(len(stamps), request)),
                Series({"__name__": "kube_pod_container_resource_requests", **labels, "resource": "memory"}, stamps,
                       np.full(len(stamps), 512 * 2**20))]
    api = by_name(SeriesProm(out), "shop")["api"]
    assert api.cpu_request_cores == 0.5 and api.replicas == 1  # the rollout's new pod, not the largest ever seen
    assert api.window_days == pytest.approx(DAYS, abs=0.1)     # one history across both ReplicaSets


# ---------- the rest of the cluster ----------

def test_the_inventory_finds_the_nodes_and_every_pods_requests(prom):
    inv = cluster.inventory(prom, catalog=CATALOG, end=END, days=DAYS)
    assert inv.at == END and inv.notes == []
    assert (inv.pool.sku, inv.pool.nodes) == ("D8s_v5", 2)
    assert inv.pods[("kube-system", next(p for ns, p in inv.pods if ns == "kube-system" and p.startswith("metrics-server")))] == {
        "metrics-server": (0.1, 200.0)}
    web = next(c for (ns, pod), c in inv.pods.items() if ns == "staging" and pod.startswith("checkout-web"))
    assert web == {"app": (0.25, 256.0), "envoy": (0.05, 64.0)}
    assert not any(pod.startswith("node-problem-detector") for _, pod in inv.pods)  # it requests nothing


def test_background_is_everything_the_analysis_does_not_resize(prom):
    inv = cluster.inventory(prom, catalog=CATALOG, end=END, days=DAYS)
    everything = sum(c for containers in inv.pods.values() for c, _ in containers.values())
    demo = list(by_name(prom, "demo").values())
    rest = cluster.background(inv, demo)
    assert sum(c for c, _ in rest) == pytest.approx(everything - sum(s.cpu_request_cores * s.replicas for s in demo), abs=0.01)
    # Analysing staging too takes its main containers out and leaves the envoy sidecar in.
    staging = list(by_name(prom, "staging").values())
    both = cluster.background(inv, demo + staging)
    assert (0.05, 64.0) in both
    assert sum(c for c, _ in rest) - sum(c for c, _ in both) == pytest.approx(
        sum(s.cpu_request_cores * s.replicas for s in staging), abs=0.01)
    assert cluster.background(cluster.Inventory(), demo) == []


def test_mixed_nodes_keep_the_configured_pool_and_say_so(series):
    relabelled = [Series({**s.labels, "label_kubesage_io_sku": "F8s_v2", "label_node_kubernetes_io_instance_type": "Standard_F8s_v2"},
                         s.timestamps_ms, s.values)
                  if s.labels["__name__"] == "kube_node_labels" and s.labels["node"].endswith("1") else s for s in series]
    inv = cluster.inventory(SeriesProm(relabelled), catalog=CATALOG, end=END, days=DAYS)
    assert inv.pool is None and any("not one catalog SKU (D8s_v5, F8s_v2)" in n for n in inv.notes)


def test_a_database_without_pod_inventory_is_a_note_not_an_error(series):
    usage_only = SeriesProm([s for s in series if s.labels["__name__"].startswith("container_")])
    inv = cluster.inventory(usage_only, catalog=CATALOG, end=END, days=DAYS)
    assert inv.at is None and inv.pods == {} and inv.pool is None
    assert any("no pod inventory" in n for n in inv.notes)


def test_history_that_ended_a_while_ago_is_read_at_its_last_hour(series):
    later = END + 5 * 3600 + 120  # the simulation was written five hours before the analysis
    inv = cluster.inventory(SeriesProm(series), catalog=CATALOG, end=later, days=DAYS)
    assert END - 3600 < inv.at <= END and len(inv.pods) > 20 and inv.pool.nodes == 2


# ---------- one analysis, several namespaces ----------

@pytest.fixture
def analysed(prom, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PromClient", lambda vm: prom)
    monkeypatch.setattr(service.time, "time", lambda: float(END))

    def run(namespace):
        result, run_dir, _ = service.analyze_now(vm="http://vm", namespace=namespace, days=DAYS, tz=TZ, mode="mock",
                                                 out_dir=tmp_path / namespace.replace("*", "all").replace(",", "+"))
        return result, run_dir
    return run


def test_every_namespace_is_analysed_in_one_run(analysed):
    result, run_dir = analysed("*")
    assert result.namespaces == ["data", "demo", "platform", "staging"]  # not the shared kube-system and monitoring
    assert result.namespace == "data,demo,platform,staging"
    ids = [w.id for w in result.workloads]
    assert "staging/checkout-web" in ids and "demo/checkout-web" in ids and "data/orders-db" in ids
    files = sorted(f.name for f in (run_dir / "manifests").iterdir())
    assert all(f.split(".")[0] in result.namespaces for f in files)   # <namespace>.<name>.<what>.yaml
    assert {"demo.checkout-web.patch.yaml", "staging.checkout-web.patch.yaml"} <= set(files)
    patch = yaml.safe_load((run_dir / "manifests" / "staging.checkout-web.patch.yaml").read_text())
    assert patch["metadata"] == {"name": "checkout-web", "namespace": "staging"}
    assert [c["name"] for c in patch["spec"]["template"]["spec"]["containers"]] == ["app"]  # never the sidecar
    pool = result.node_pool
    assert (pool.before_sku, pool.before_nodes) == ("D8s_v5", 2)          # the cluster's nodes, not config/skus.yaml
    assert pool.background_pods == 6 and pool.background_cpu_cores == pytest.approx(0.95)
    assert any("does not resize" in n for n in pool.notes)
    kinds = {yaml.safe_load((run_dir / "manifests" / f).read_text())["kind"] for f in files if f.startswith("data.orders-db.")}
    assert kinds == {"StatefulSet"}
    # Black Friday is declared for demo's storefront, so staging's copy gets no dated trigger.
    staging_keda = run_dir / "manifests" / "staging.checkout-web.keda.yaml"
    assert not staging_keda.exists() or "dated-events" not in staging_keda.read_text()
    steps = apply.plan(run_dir, kubectl=["kubectl"])
    commands = [" ".join(s.cmd[:6]) for s in steps]
    assert "kubectl -n data patch statefulset orders-db" in commands
    assert "kubectl -n staging patch deployment checkout-web" in commands
    with pytest.raises(gitops.GitOpsError, match="covers 4 namespaces"):
        gitops.export(run_dir, run_dir, path="x", kustomize_base="../base")


def test_one_namespace_keeps_its_file_names_and_counts_the_rest_of_the_cluster(analysed):
    result, run_dir = analysed("demo")
    assert result.namespace == "demo" and result.namespaces == ["demo"]
    assert "checkout-web.patch.yaml" in {f.name for f in (run_dir / "manifests").iterdir()}
    pool = result.node_pool
    assert pool.before_nodes == 2 and pool.background_pods > 10  # the fleet around demo is on the same nodes
    packed, _ = analysed("demo,staging")
    assert packed.namespaces == ["demo", "staging"] and packed.node_pool.background_pods < pool.background_pods
    report = json.loads((run_dir / "report.json").read_text())
    assert report["namespaces"] == ["demo"] and "background_pods" in report["node_pool"]


def test_namespace_selection(prom, monkeypatch):
    pick = lambda value: service.target_namespaces(value, prom, days=DAYS, end=END)
    assert pick("demo") == ["demo"] and pick(" demo, staging ,demo") == ["demo", "staging"]
    assert pick("*") == ["data", "demo", "platform", "staging"]
    with pytest.raises(service.AnalysisError, match="no workloads found in namespace 'nowhere'"):
        service.collect_stats(vm="http://vm", namespace="nowhere", days=DAYS, tz=TZ, client=prom, end=END)
    stats, _ = service.collect_stats(vm="http://vm", namespace="nowhere,data", days=DAYS, tz=TZ, client=prom, end=END)
    assert {s.namespace for s in stats} == {"data"}


# ---------- manifests for the kinds and names found ----------

REC = Recommendation(workload="data/db", diagnosis="x", risk="low",
                     resources=ResourceSpec(cpu_request_m=500, cpu_limit_m=None, mem_request_mi=1024, mem_limit_mi=2048),
                     scaling=ScalingPlan(min_replicas=1, max_replicas=3, cpu_utilization_target=70,
                                         cron_windows=[CronWindow(start="0 8 * * 1-5", end="0 18 * * 1-5", desired_replicas=2)]))


def test_a_statefulset_gets_a_statefulset_patch_and_scale_target():
    files = render.render(REC, namespace="data", name="db", container="postgres", tz="UTC", kind="StatefulSet", stem="data.db")
    assert set(files) == {"data.db.patch.yaml", "data.db.keda.yaml"}
    patch, so = yaml.safe_load(files["data.db.patch.yaml"]), yaml.safe_load(files["data.db.keda.yaml"])
    assert (patch["kind"], patch["metadata"]) == ("StatefulSet", {"name": "db", "namespace": "data"})
    assert patch["spec"]["template"]["spec"]["containers"][0]["name"] == "postgres"
    assert so["spec"]["scaleTargetRef"] == {"kind": "StatefulSet", "name": "db"} and so["metadata"]["name"] == "db"
    deployment = yaml.safe_load(render.render(REC, namespace="data", name="db", container="db", tz="UTC")["db.keda.yaml"])
    assert deployment["spec"]["scaleTargetRef"] == {"name": "db"}  # KEDA's default kind, as before
    assert yaml.safe_load(render.migration("data", "db", "sku", "F4s_v2", "StatefulSet"))["kind"] == "StatefulSet"


def test_apply_patches_the_kind_the_file_names_and_refuses_any_other(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    fixed = Recommendation(workload="data/db", diagnosis="x", risk="low", scaling=ScalingPlan(min_replicas=2, max_replicas=2))
    for name, text in {**render.render(REC, namespace="data", name="db", container="db", tz="UTC", kind="StatefulSet", stem="data.db"),
                       **render.render(fixed, namespace="shop", name="web", container="web", tz="UTC", stem="shop.web")}.items():
        (manifests / name).write_text(text)
    steps = apply.plan(tmp_path, kubectl=["kubectl"])
    commands = [" ".join(s.cmd[:6]) for s in steps]
    assert commands[0] == "kubectl -n data patch statefulset db"
    # shop/web is pinned to two replicas and has no ScaledObject beside it under its stem: an old one is removed.
    assert any(c.startswith("kubectl -n shop delete scaledobjects.keda.sh") for c in commands)
    assert not any(c.startswith("kubectl -n data delete") for c in commands)  # data.db has data.db.keda.yaml
    (manifests / "x.patch.yaml").write_text(yaml.safe_dump({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "n"}}))
    with pytest.raises(ValueError, match="cannot patch a Secret"):
        apply.plan(tmp_path, kubectl=["kubectl"])


def test_the_model_is_only_told_what_is_unusual():
    from kubesage.pipeline import _compact

    base = dict(WorkloadStats.model_validate_json(json.dumps(_stats_record())).model_dump())
    plain = _compact(WorkloadStats(**base))
    assert "kind" not in plain and "sidecars" not in plain and "container" not in plain
    odd = _compact(WorkloadStats(**{**base, "kind": "StatefulSet", "container": "postgres", "sidecars": ["exporter"]}))
    assert (odd["kind"], odd["container"], odd["sidecars"]) == ("StatefulSet", "postgres", ["exporter"])


def _stats_record() -> dict:
    client = SeriesProm(cluster_series(1))
    stats, _ = collect(client, namespace="data", days=1, end=END, tz=TZ)
    return json.loads(next(s for s in stats if s.name == "redis-cache").model_dump_json())


def test_fleet_exports_what_right_sizing_reads():
    names = {s.labels["__name__"] for s in fleet.generate_fleet(days=1, end=END, tz=TZ)}
    assert {"kube_deployment_spec_replicas", "kube_statefulset_replicas", "kube_pod_owner", "kube_replicaset_owner"} <= names
