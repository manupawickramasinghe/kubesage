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

`analyze` writes `out/<timestamp>/report.json` and `out/<timestamp>/manifests/*.yaml`:

```bash
kubectl -n demo patch deployment payment-service --type strategic --patch-file out/<ts>/manifests/payment-service.patch.yaml
kubectl apply -f out/<ts>/manifests/report-worker.keda.yaml
```

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
config/       model tiers + SKU catalog
deploy/       Grafana provisioning + dashboard
```

## Roadmap

- [ ] kind/k3s cluster + KEDA + demo Deployments matching the scenarios; `kubesage apply`
- [ ] Live mode: vmagent scraping the cluster, plus a "fast-forward" load spike for the demo
- [ ] Web UI ("AI Control Center") on top of `report.json`, with the router-trace badges
- [ ] GitOps: open a PR with the manifests instead of applying directly
- [ ] Two-node mode: label nodes as SKUs and migrate pods to the cheap node live
