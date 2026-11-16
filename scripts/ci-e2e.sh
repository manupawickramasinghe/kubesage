#!/usr/bin/env bash
# End-to-end check against a real Prometheus-compatible database, with no cluster and no model call:
# simulate 30 days of a small cluster, allocate its costs, check the budgets and right-size one namespace.
#   scripts/ci-e2e.sh [VM_URL]        default http://localhost:8428
# Used by .github/workflows/tests.yml; run it locally against `docker compose up -d victoriametrics`.
set -euo pipefail
cd "$(dirname "$0")/.."
VM=${1:-http://localhost:8428}
KUBESAGE=${KUBESAGE:-.venv/bin/kubesage}
PY=$(dirname "$KUBESAGE")/python
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
export KUBESAGE_RING="$OUT/ring" KUBESAGE_RING_MB=4

for _ in $(seq 60); do curl -fs "$VM/health" >/dev/null && break; sleep 1; done
curl -fs "$VM/health" >/dev/null || { echo "no database at $VM" >&2; exit 1; }

"$KUBESAGE" simulate --vm "$VM"
"$KUBESAGE" costs --vm "$VM" --json > "$OUT/costs.json"
"$PY" - "$OUT/costs.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
c = r["cluster"]
assert r["observed_days"] == 30, r["observed_days"]
assert abs(c["nodes_monthly_usd"] - 560.64) < 0.5, c                      # two D8s_v5 at list price
assert {l["name"] for l in r["by_namespace"]} == {"demo", "data", "staging", "platform", "monitoring", "kube-system"}
loaded = sum(l["monthly_loaded_usd"] for l in r["by_namespace"] if not l["shared"])
assert abs(loaded - c["monthly_usd"]) < 1, (loaded, c["monthly_usd"])      # the loaded column is the whole bill
found = {(f["kind"], f["target"]) for f in r["findings"]}
for want in [("idle-workload", "data/legacy-export"), ("unused-volume", "data/orders-db-backup-2025"),
             ("no-requests", "platform/node-problem-detector"), ("cost-spike", "staging"), ("idle-capacity", "cluster")]:
    assert want in found, (want, sorted(found))
kinds = {(l["namespace"], l["name"]): l["kind"] for l in r["by_workload"]}
assert kinds[("data", "orders-db")] == "StatefulSet" and kinds[("demo", "checkout-web")] == "Deployment", kinds
print(f"costs: ${c['monthly_usd']}/mo, {c['idle_pct']}% idle, {len(r['findings'])} findings, "
      f"${r['potential_monthly_usd']}/mo to check")
PY
"$KUBESAGE" costs --vm "$VM" --prometheus | grep -q '^kubesage_cluster_monthly_cost_usd '
if "$KUBESAGE" budgets --vm "$VM"; then echo "expected the staging budget to be over" >&2; exit 1; fi

"$KUBESAGE" analyze --vm "$VM" --mode mock --out-dir "$OUT/runs" > /dev/null
RUN=$(ls -d "$OUT"/runs/*/ | tail -1)
grep -q 'kubesage.io/dated-events' "$RUN/manifests/checkout-web.keda.yaml"
! ls "$RUN"/manifests/*.timed.yaml >/dev/null 2>&1
"$KUBESAGE" realized --vm "$VM" --out-dir "$OUT/runs" > /dev/null

# The whole cluster in one run: workloads found through their owners, the node pool read from the real nodes.
"$KUBESAGE" analyze --vm "$VM" --all-namespaces --mode mock --out-dir "$OUT/all" > /dev/null
ALL=$(ls -d "$OUT"/all/*/ | tail -1)
"$PY" - "$ALL" "$RUN" <<'PY'
import json, pathlib, sys, yaml
run, one = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
r = json.loads((run / "report.json").read_text())
assert r["namespaces"] == ["data", "demo", "platform", "staging"], r["namespaces"]   # not kube-system, not monitoring
stats = {s["id"]: s for s in r["stats"]}
assert stats["data/orders-db"]["kind"] == "StatefulSet", stats["data/orders-db"]["kind"]
web = stats["staging/checkout-web"]
assert (web["container"], web["sidecars"]) == ("app", ["envoy"]), web        # the main container, not the proxy
assert "platform/node-problem-detector" not in stats                           # a DaemonSet is not right-sized
pool = r["node_pool"]
assert (pool["before_sku"], pool["before_nodes"]) == ("D8s_v5", 2), pool      # the cluster's nodes, not the config's
assert pool["background_pods"] == 6, pool["background_pods"]                  # kube-system, monitoring, the sidecar
patch = yaml.safe_load((run / "manifests" / "staging.checkout-web.patch.yaml").read_text())
assert patch["spec"]["template"]["spec"]["containers"][0]["name"] == "app", patch
assert yaml.safe_load((run / "manifests" / "data.orders-db.migrate.yaml").read_text())["kind"] == "StatefulSet"
# One namespace alone still counts everything else on the nodes.
single = json.loads((one / "report.json").read_text())["node_pool"]
assert single["before_nodes"] == 2 and single["background_pods"] > 10, single
print(f"whole cluster: {len(stats)} workloads in {len(r['namespaces'])} namespaces, pool {pool['before_nodes']}x "
      f"{pool['before_sku']} -> {pool['choice']['nodes']}x {pool['choice']['sku']}, {pool['background_pods']} pods left alone")
PY
"$KUBESAGE" whatif --vm "$VM" --multiplier 5 --json > /dev/null
echo "end-to-end: ok"
