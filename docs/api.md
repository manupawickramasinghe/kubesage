# Web API (`kubesage serve`)

JSON over HTTP on port 8600. Every POST must carry the header `X-KubeSage: 1`; without it the server answers
403. Browsers can't add that header cross-origin without a CORS preflight, which this server never answers, so
other websites can't trigger actions. There's no authentication beyond that.

| method | path | does |
|---|---|---|
| GET | `/` | the UI (`kubesage/web/static/index.html`) |
| GET | `/fonts/<file>.woff2` | vendored fonts, cached for a day; only files in `static/fonts` |
| GET | `/api/config` | `{namespace, mode: "live"\|"mock", tiers: {triage: model, …}, gitops: bool, operation}` |
| GET | `/api/reports` | Up to 50 completed runs, newest first, for the configured namespace: `{runs: [{run, generated_at, mode, namespace, llm_cost_usd, monthly_savings_usd}]}`. Savings can be negative. |
| GET | `/api/report?run=<id>` | `{run, report}` for a selected run; omit `run` for the latest valid report. 404 before the first analysis. |
| GET | `/api/cluster` | live nodes, pods, scaling (see below), or `{available: false}` |
| POST | `/api/analyze` | Runs the full pipeline and atomically stores a report and manifests: `{run, report}`. 502 for metrics or LLM errors. |
| POST | `/api/apply` | `{run?, dry_run: bool}` → `{run, dry_run, ok, error, steps: [{file, ok, skipped, output}]}`. Selected run is pinned for the whole operation. An omitted `run` means the newest run, and is refused if that run belongs to another namespace: writes never fall back to an older run. |
| POST | `/api/spike` | `{rps 1–200, work_ms 1–500, seconds 10–900, target?}` → `{job, TARGET, RPS, WORK_MS, SECONDS}` |
| POST | `/api/reset` | runs `scripts/demo-reset.sh` → `{ok, output: [last lines], error?}`; only available for namespace `demo` |
| GET | `/api/lab` | Live status for the lab's dataflow: `{vm: {url, ok, series, last_sample_age_s}, host, pid, namespace, mode, provider: {base_url, api_key_env, profile}, tiers: {name: {label, model, input_per_mtok, output_per_mtok, extra}}}`. Never includes a key value. |
| GET | `/api/events?after=<id>` | The journal: `{events: [{id, at, unit, stage, level, msg}], last}`, up to 500 newer than `after`. Also reads the namespace's Kubernetes events, at most every 3 s, each journalled once (only the last 10 minutes on first read). |
| GET | `/api/trace?run=<id>` | What a run sent: `{run, mode, exchanges: [...trace.json], kubectl: [{file, cmd, manifest}], backfill: [...] \| null}`. `exchanges` is empty for runs from before tracing. |
| POST | `/api/shell` | `{cmd}` → `{cmd, exit, out, ms}`. Read-only allow-list (see the UI guide); refused commands exit 2 without running anything. |
| POST | `/api/backfill` | `{days: 1–60}` → `{samples, series, days, seconds}`. Regenerates the demo history in VictoriaMetrics, replacing the namespace's series. Holds the operation lock; demo namespace only. |
| POST | `/api/gitops` | `{run?, open_pr: bool}` → `{branch, commit, pushed, pr_url}`; 400 unless started with `--gitops-repo`/`--gitops-base` |
| GET | `/api/testcases` | `{catalog: [{name, title, category, app, summary, expect, gitops, load_rps}], active: {name: {pods, phases, restarts, since, stopping}}, alerts: [{name, severity, summary, since, pod, node}], cluster: bool}` |
| POST | `/api/testcase` | `{action: "start"\|"stop"\|"stop-all", name}` → `{ok, steps: [{cmd, ok, output}], error}`. Names come from the catalog only. 409 while another start/stop runs. |
| GET | `/api/agents` | The agent team: `{team: {profile, provider: {base_url, api_key_env}, agents: [{name, title, role, size, label, model, description, folder}], rules: [{file, text}], error}, run: <latest run>, runs: [...], auto, prs, pending: {alert, in_s} \| null}` |
| POST | `/api/agents/run` | `{trigger?: "manual"}` → 202 `{id}`; the run continues in the background. 409 if one is running. |
| GET | `/api/agents/run?id=<id>` | One run: per-agent state, tokens, cost; every sub-agent's signals and report; the diagnosis; what validation rejected; the executor's branch, commit, edits, diff, PR body and `pr_url`. |
| POST | `/api/agents/apply` | `{id, dry_run: bool}` → `{ok, dry_run, steps: [{cmd, ok, output}], skipped: [{path, reason}]}`: `kubectl apply -k` of each changed `gitops/apps/<app>` from the run's commit. Terraform files are listed as skipped. |
| POST | `/api/agents/pr` | `{id}` → `{pr_url}`: push the run's branch and open a PR with `gh`. 403 unless started with `--agent-prs`. |
| POST | `/api/agents/auto` | `{on: bool}` → `{auto}`: run the team when a lab alert fires |
| GET | `/api/ring?after=<seq>&streams=a,b/&limit=` | `{records: [{seq, ts, stream, text}], last}` from the RAM ring; `tail=<n>` instead of `after` returns the newest n. A trailing `/` or `*` matches a stream prefix. |
| GET | `/api/ring/stat` | `{path, capacity_bytes, used_bytes, segments, segments_used, wrapped, records, last_seq, oldest_ts, newest_ts}` |
| GET | `/api/ring/dump?minutes=<n>&streams=` | The ring as a `text/plain` download (0 minutes: everything held), API key values redacted |

The ring has no write endpoint, and none of these endpoints gives the browser a terminal. Every response is
recorded in an in-memory request log that the security agent reads; 403s, 404s and refused shell commands also
go to the ring's `access` stream.

FinOps mutations share a lock: overlapping analyze, apply, spike, reset, backfill or GitOps requests return 409.
Agent run/PR/Apply operations and testcase operations use separate locks; there is no global or cross-process
serialization across these groups. Request bodies must be JSON objects of at most 64 KiB. Invalid parameter
types or ranges return 400.

Apply skips migrations when the target SKU lacks the planned number of Ready, schedulable nodes. A skipped
step is reported even when the response is 200; `ok` does not mean every manifest was written. A failed or
timed-out command returns 502 with its step output, and dependent remaining steps are skipped. A dry run
validates the steps it executes with Kubernetes without saving changes; skipped steps remain unvalidated.

## `GET /api/cluster`

```jsonc
{
  "available": true,
  "namespace": "demo",
  "metrics": true,                      // metrics-server answered for the namespace's pods
  "nodes": [{
    "name": "manupa-hnx", "role": "worker", "ready": true, "created": "2026-10-27T16:12:00Z",
    "host": "manupa-hnx", "cpu_model": "AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx",
    "os": "Ubuntu 26.04.1 LTS", "kernel": "7.0.0-38-generic", "kubelet": "v1.36.4+k3s1",
    "runtime": "containerd://2.3.4-k3s1.36", "k3d": false,
    "internal_ip": "192.168.1.92", "external_ip": "192.168.1.92",
    "cpu_cores": 8.0, "mem_mi": 14404,          // real capacity
    "alloc_cpu_m": 8000, "alloc_mem_mi": 14404, // what pods can request
    "cpu_used_m": 1364, "mem_used_mi": 2322,    // whole node now (metrics-server); for k3d nodes, the container
    "pool": "F8s_v2", "pool_monthly_usd": 246.74, // the cost model, not the hardware
    "pods": [{
      "name": "checkout-web-69bd755646-j7m2v", "app": "checkout-web", "phase": "Running", "ready": true,
      "cpu_request_m": 760, "mem_request_mi": 280, "mem_limit_mi": 448,
      "cpu_used_m": 123, "mem_used_mi": 13.4, "restarts": 0
    }]
  }],
  "scaling": {"checkout-web": {"min": 1, "max": 3, "triggers": ["cron", "cpu"], "current": 3, "desired": 3,
                               "cpu": 10, "cpu_target": 70}}
}
```

Unscheduled pods are listed under a pseudo-node named `(pending)` with role `pending`. `host`, `cpu_model` and
`os` come from the `kubesage.io/host`, `kubesage.io/cpu` and `kubesage.io/os` node annotations, which
`cluster-up.sh` and `join-node.sh` set.

The response also includes:

- `observed_at` and `operation` (the active mutation's name and start time, or null);
- `deployments`, keyed by name: desired/ready/updated/available replicas, node selector and live resource
  requests/limits;
- `plan`: comparison of the **latest** analysis's Deployment and ScaledObject fields with live objects.
  `state` is `none`, `pending`, `partial`, `applied` or `unknown`; `matched`/`total` count fully matching
  workloads. `ready` additionally checks Deployment rollout readiness. `workloads[name].checks[]` contains
  `{field, expected, actual, match}` for drift inspection. `capacity` reports `{sku, required, ready, available}`;
- `active_jobs[]`: running or pending KubeSage traffic jobs, including target, load parameters and expected
  `ends_at`. CPU targets and HPA conditions are included under `scaling` when available.
- `traffic`: the latest managed traffic job, including completed/failed jobs, or null. It contains `job`,
  `status`, `succeeded_requests`, `failed_requests`, `p50_ms`, `p95_ms`, `elapsed` and `logs_available`.
  Request counters sum the load generator's interval reports; latency comes from its latest interval.
  Missing results are null, not zero. Logs reads are bounded and cached for ten seconds; raw logs are never
  returned. A completed Kubernetes Job can still contain failed HTTP requests.

Plan verification comes from Kubernetes, so reloading the page or restarting the server retains it. It is
separate from a browser's last dry-run result. Historical report selection does not change which run the
cluster endpoint verifies. Cluster samples are shared across tabs for two seconds.

Reports and writes are restricted to the configured namespace. Invalid or mismatched pinned runs return
400 before any writes. Known credential values are redacted from outgoing JSON, including error output.

## `trace.json`

Written next to `report.json` by each analysis: a list of exchanges in the order they happened.

- `{kind: "promql", stage: 1, request: {method, url, params}, response: {status, series, first_series}, latency_ms}`
- `{kind: "llm", stage: 2|3, llm_stage, workload, tier, label, model, attempt, sent, request: {method, url, headers, body},
  response: {status, body} | {note, body}, latency_ms}`. The `Authorization` header is recorded as
  `Bearer $<API_KEY_ENV>`, never the key. In mock mode `sent` is false and the body is the deterministic answer.

## `report.json`

Written to `out/<run>/report.json` and returned by `/api/report` and `/api/analyze`:

- `generated_at`, `mode`, `namespace`, `window_days`, `tiers` (tier → display label);
- `stats[]`: the Stage 1 record per workload (see [pipeline.md](pipeline.md));
- `workloads[]`:
  - `id`, `triage {status, summary}`, `tier`;
  - `recommendation {diagnosis, risk, resources, scaling, follow_ups}`;
  - `guardrail_notes[]`, `monthly_before_usd`, `monthly_after_usd`, `manifests {file: yaml}`;
- `node_pool`:
  - `before_sku`, `before_nodes`, `before_monthly_usd`;
  - `choice {sku, nodes, rationale}`, `after_monthly_usd`;
  - `candidates[]`, `demand_before`, `demand_after`, optional `demand_burst`;
  - `node_label`, `migrations {file: yaml}`, `notes[]`;
- `ledger[]`: `{stage, workload, tier, model, input_tokens, cached_tokens, output_tokens, cost_usd}`;
- `llm_cost_usd`, `baselines_usd {all_calls_on_strategist, raw_series_to_strategist}`, `notes[]`.

Each run is an immutable snapshot, published only after all files are complete. Node sizing uses scheduled
peaks (minimum/cron targets); maximum CPU autoscaler demand is checked separately as `demand_burst`, with
fit/shortfall notes. This is an aggregate model, not a scheduler or a guarantee that all scale-out pods fit.
Workload costs use scheduled average replicas, with overlapping targets combined by their maximum and
unpredictable CPU scale-out excluded. The UI downloads the report as JSON and manifests as a YAML bundle.
See the [current review](REVIEW_REPORT.md) for unresolved policy, state and evidence gaps.
