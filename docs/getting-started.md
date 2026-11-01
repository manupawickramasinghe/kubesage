# Getting started on your own machine

Three ways in, from "just look at it" to "run it on my cluster". All three work on Linux/macOS with
Python 3.11+; only Path A needs Docker. Nothing here needs our machines, our cluster or our keys.

## Path A — see it work (5 minutes, no cluster, no API key)

```bash
git clone https://github.com/manupawickramasinghe/kubesage && cd kubesage
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d                    # VictoriaMetrics :8428 + Grafana :3300 (metrics only)
.venv/bin/kubesage simulate             # 30 days of synthetic history (~2 s)
.venv/bin/kubesage analyze              # mock models: full pipeline, $0, writes out/<run>/
.venv/bin/kubesage serve                # dashboard at http://127.0.0.1:8600
```

Mock mode is deterministic and clearly labelled in the UI. It exercises everything — statistics,
forecast, guardrails, manifests, node-pool math — except real model judgement.

## Path B — your Meta Muse key (real model reasoning)

KubeSage's default profile *is* Meta Muse on the Meta Model API — no profile to select.
Get a key at [dev.meta.ai](https://dev.meta.ai) (keys look like `LLM|...`), then:

```bash
cp .env.example .env                    # then edit it (it is gitignored; chmod 600 it)
# in .env set:  MODEL_API_KEY=<your Meta Model API key>   (leave KUBESAGE_MODELS blank)
set -a; . ./.env; set +a
.venv/bin/kubesage analyze --mode live  # live is also auto-detected when the key is set
.venv/bin/kubesage serve                # dashboard at http://127.0.0.1:8600
```

What runs: triage and routine work on **Muse Spark 1.3** (`muse-spark-1.3`, $1.25/$4.25 per 1M
tokens), hard cases and the node-pool plan on **Muse Wonder 1.0** (`muse-wonder-1.0`,
$4.00/$20.00). That split is the cost-aware router: only workloads Spark flags as COMPLEX ever
reach Wonder, and every run prints its exact token cost from these prices. Expect a few cents
per full Analyze — the report shows the real figure, not an estimate. Note there is no hosted
Glimmer tier: Muse Glimmer is open-weight only and is not served on `api.meta.ai`.

Want cheaper triage? Set the triage tier's model in `config/models.yaml` to
`muse-spark-1.3-contributor` ($0.10/$0.20) — Contributor terms let Meta train on inputs,
so only do this if that is acceptable for cluster-statistics payloads.

**Cerebras instead:** set `CEREBRAS_API_KEY` in `.env` and run with
`KUBESAGE_MODELS=cerebras` (a full Analyze costs about **$0.01** there).

**A different provider** (OpenAI, Together, local vLLM, …): any OpenAI-compatible
`/chat/completions` endpoint works. Copy the profile and edit four things — this is the
whole integration surface:

```bash
cp config/models.cerebras.yaml config/models.mine.yaml
# edit: provider.base_url, provider.api_key_env (the env var holding YOUR key),
#       tiers.*.model (IDs your provider serves), tiers.* prices (for honest cost display)
KUBESAGE_MODELS=mine .venv/bin/kubesage analyze
```

The three tier keys (`triage`, `worker`, `strategist`) are fixed; the client asks for JSON output and
retries rate limits and schema-invalid answers.

## Path C — your cluster, your metrics

KubeSage reads metrics; it does not install an agent in your cluster. You need:

1. **A Prometheus or VictoriaMetrics** holding ~30 days (less works, forecasts get less confident):
   - `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes` (cAdvisor)
   - `kube_pod_container_status_restarts_total`, `kube_pod_container_resource_requests|limits`
     (kube-state-metrics)
2. **Point at it and at your kubeconfig:**
   ```bash
   export KUBESAGE_VM_URL=http://your-prometheus:9090 KUBECONFIG=~/.kube/config
   export KUBESAGE_TZ=Europe/London        # YOUR users' timezone — patterns/crons use it
   .venv/bin/kubesage stats --namespace <your-namespace>     # $0, no LLM: sanity-check Stage 1
   .venv/bin/kubesage analyze --namespace <your-namespace>   # then the full pipeline
   ```
3. **Make the cost model yours** (`config/skus.yaml`): replace the Azure-style catalog with your
   instance types and prices, and set `current_pool` to what you run today. Savings are computed
   against that pool — with our SKUs left in, your numbers would be fiction.
4. **Make the calendar yours** (`config/calendar.yaml`): declare *your* working hours, sales and
   holidays with *your* traffic multipliers. This file is the only source of future dates — the
   models may never invent an event. Delete our demo events; see [calendar.md](calendar.md).
   An existing Google/Outlook calendar can be drafted in with the .ics importer
   (`kubesage.calendar_import.parse_ics`), and recurring peaks you haven't declared are proposed —
   never added — by `kubesage.calendar_suggest`.

Prefer to run KubeSage itself in the cluster? [in-cluster.md](in-cluster.md) deploys it as a
container (CronJob analysis + UI) with read-only RBAC, where Flux applies merged changes.

Apply stays a deliberate step: `kubesage apply --dry-run` first, and the agent team's Executor only
ever opens pull requests when started with `--agent-prs`. Nothing writes to your cluster on its own.

## What it costs to run

| thing | cost |
|---|---|
| Mock mode (no key) | $0 |
| Live Analyze (Meta Muse: Spark + Wonder) | a few cents per run; exact cost printed per run |
| Live Analyze (Cerebras) | ~$0.01 per run |
| VictoriaMetrics/Grafana (compose) | local Docker only |
| The demo cluster (`scripts/cluster-up.sh`) | local k3d; optional, Path C replaces it |

## Troubleshooting

| symptom | likely cause |
|---|---|
| `analyze` finds no workloads | VM has no data for that namespace/series — run `kubesage stats` and check the series names above; if you used the compose stack, did `simulate` finish? |
| Everything says "mock" despite a key | the profile's `api_key_env` variable isn't in the *shell* environment — `set -a; . ./.env; set +a`, and check `KUBESAGE_MODELS` names the profile you edited |
| 401/404 from the model endpoint | `MODEL_API_KEY` missing/invalid, or `KUBESAGE_MODELS` set to a profile you didn't mean (leave it blank for Meta Muse). A 404 model means the ID isn't served — on Meta only `muse-spark-1.3` and `muse-wonder-1.0` are; Glimmer is open-weight only |
| Forecasts mention Black Friday in your June report | our demo `config/calendar.yaml` is still in place — declare your own events (Path C, step 4) |
| Savings look huge/tiny | `config/skus.yaml` still prices our machines — set your instance types and `current_pool` (Path C, step 3) |
| `apply` can't reach the cluster | `KUBECONFIG` unset and no `.bin/kubeconfig` (that's only created by `scripts/cluster-up.sh`) |
| Port 8600/8428 already in use | `--port` for serve; for compose, `BIND_ADDR`/host port mapping in `docker-compose.yml` |

Something missing here? The [configuration reference](configuration.md) lists every file and
environment variable; [operations.md](operations.md) is the runbook.
