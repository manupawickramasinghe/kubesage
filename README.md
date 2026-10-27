# KubeSage

AI FinOps agent for Kubernetes. It reads weeks of usage history and finds over-provisioned requests,
idle workloads that can scale to zero, recurring traffic peaks, and memory leaks. For each one it outputs
right-sized resource patches, proactive KEDA `ScaledObject`s and a cheaper node-pool SKU, with the dollar math.

It also tracks its own LLM bill: a 3-stage router only sends a workload to an expensive model when a
cheap one says it needs it.

```
 Prometheus / VictoriaMetrics
            │  30 days of cAdvisor + kube-state-metrics series
            ▼
 Stage 1  deterministic stats ─────────────── $0, no LLM: p50/p95/p99, hourly profiles,
            │                                 recurring peaks, leak slope, restarts
            ▼
 Stage 2  triage (Muse Glimmer) ──────────── one cheap call for all workloads
            │   HEALTHY → stop    ROUTINE → Spark    COMPLEX → Wonder
            ▼
 Stage 3  remediation (Muse Spark / Wonder) ─ returns parameters, not YAML
            │
            ▼
 guardrails (never below observed usage) → YAML renderer → cost model → node-pool plan (Wonder)
```

Models return structured parameters, never raw YAML. Deterministic guardrails clamp anything below observed
usage, and every clamp is reported, so a bad model answer can't OOM-kill a pod.

## Quickstart

Requirements: Docker, Python 3.11+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d                 # VictoriaMetrics :8428, Grafana :3300
.venv/bin/kubesage simulate          # backfill 30 days of synthetic history (~1.4M samples, ~2 s)
.venv/bin/kubesage stats             # Stage 1 only, no LLM
.venv/bin/kubesage analyze           # full pipeline; mock mode unless MODEL_API_KEY is set
```

Grafana: http://localhost:3300 (anonymous view, admin/admin to edit) opens the "requested vs used" dashboard.

`analyze` writes `out/<timestamp>/report.json` and `out/<timestamp>/manifests/*.yaml`.

### Local cluster + apply

A one-node [k3d](https://k3d.io) cluster with KEDA, running the five demo Deployments in their oversized
"before" state (pause containers, so the requests reserve capacity but use none). Adds ~600 MiB RAM.

```bash
scripts/cluster-up.sh                # downloads pinned k3d + kubectl into .bin/, creates cluster, installs KEDA
.venv/bin/kubesage apply --dry-run   # server-side dry run of the latest analysis run
.venv/bin/kubesage apply             # Deployment patches first, then ScaledObjects
scripts/demo-reset.sh                # back to the "before" state, to run the demo again
scripts/cluster-down.sh
```

**Two-node SKU mode.** `TWO_NODE=1 scripts/cluster-up.sh` adds an agent node (or upgrades a running one-node
cluster). The server node is labelled `kubesage.io/sku=D8s_v5` (the current pool) and the agent `F4s_v2`.
The demo pods start pinned to the expensive node. When the node-pool step picks a cheaper SKU, `analyze` writes
`*.migrate.yaml` nodeSelector patches and `apply` moves every pod onto the cheap node live. Migrations are
skipped when no node carries the target label. For two physical machines (8600G + 3500U), see
[docs/two-machines.md](docs/two-machines.md).

The kubeconfig goes to `.bin/kubeconfig` and `~/.kube/config` is left alone. Use
`export KUBECONFIG=$PWD/.bin/kubeconfig PATH=$PWD/.bin:$PATH` to run kubectl yourself.
`kubesage apply` uses `$KUBECONFIG` when set, so it can target any cluster.

### Live LLM mode

```bash
export MODEL_API_KEY=...             # Meta Model API key
.venv/bin/kubesage analyze --mode live
```

Tiers, model IDs, prices and provider knobs are in [`config/models.yaml`](config/models.yaml).
**Verify the model IDs and prices there before demo day.** Glimmer/Wonder IDs and prices are unverified
placeholders. Any OpenAI-compatible endpoint works if you change `base_url`.

## The demo scenarios

`kubesage simulate` generates five workloads in namespace `demo`, each with a real-world anti-pattern:

| workload | story | expected outcome |
|---|---|---|
| `payment-service` | requests 2 CPU / 4 GiB, uses ~0.2 CPU / 470 MiB | ROUTINE → right-size |
| `report-worker` | idles all day, 45-min batch at 02:00, holds 2 cores 24/7 | ROUTINE → scale to zero + cron pre-warm |
| `checkout-web` | Mon–Fri 09:00 rush pins pods at their CPU limit | ROUTINE → cron pre-scale + CPU trigger |
| `auth-api` | leaks ~110 MiB/day, OOM-killed every ~6 days | COMPLEX → root cause, stopgap, follow-ups |
| `catalog-api` | requests match usage | HEALTHY → no tokens spent |

Series use real metric names (`container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`,
`kube_pod_container_resource_requests`, ...), so the analyzer works unchanged against a real cluster's TSDB.
The "before" node pool (3× D8s_v5) is a scenario setting in [`config/skus.yaml`](config/skus.yaml).

## Layout

```
kubesage/
  simulate/   scenario curves + VictoriaMetrics backfill
  metrics/    PromQL client, collection, Stage 1 stats
  llm/        schemas, prompts, OpenAI-compatible client with cost ledger, mock tiers
  recommend/  guardrails, cron subset, cost model, manifest renderer
  pipeline.py the router
  apply.py    kubectl apply of a run's manifests
config/       model tiers + SKU catalog
deploy/       Grafana provisioning + dashboard, demo k8s manifests
scripts/      pinned tool install, cluster up/down/reset
```

## Roadmap

- [x] k3d cluster + KEDA + demo Deployments matching the scenarios; `kubesage apply`
- [ ] Live mode: vmagent scraping the cluster, plus a "fast-forward" load spike for the demo
- [ ] Web UI ("AI Control Center") on top of `report.json`, with the router-trace badges
- [ ] GitOps: open a PR with the manifests instead of applying directly
- [x] Two-node mode: label nodes as SKUs and migrate pods to the cheap node live
