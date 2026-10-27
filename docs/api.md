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
| POST | `/api/apply` | `{run?, dry_run: bool}` → `{run, dry_run, ok, error, steps: [{file, ok, skipped, output}]}`. Selected run is pinned for the whole operation; omitted `run` uses latest. |
| POST | `/api/spike` | `{rps 1–200, work_ms 1–500, seconds 10–900, target?}` → `{job, TARGET, RPS, WORK_MS, SECONDS}` |
| POST | `/api/reset` | runs `scripts/demo-reset.sh` → `{ok, output: [last lines], error?}`; only available for namespace `demo` |
| POST | `/api/gitops` | `{run?, open_pr: bool}` → `{branch, commit, pushed, pr_url}`; 400 unless started with `--gitops-repo`/`--gitops-base` |

Mutations share a lock: overlapping analyze, apply, spike, reset or GitOps requests return 409. Request bodies
must be JSON objects of at most 64 KiB. Invalid parameter types or ranges return 400.

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
  - `candidates[]`, `demand_before`, `demand_after`;
  - `node_label`, `migrations {file: yaml}`, `notes[]`;
- `ledger[]`: `{stage, workload, tier, model, input_tokens, cached_tokens, output_tokens, cost_usd}`;
- `llm_cost_usd`, `baselines_usd {all_calls_on_strategist, raw_series_to_strategist}`, `notes[]`.

Each run is an immutable snapshot, published only after all files are complete. Node sizing includes CPU
autoscalers at their maximum replicas; workload costs use scheduled average replicas with overlapping cron
targets combined by their maximum. The model can therefore recommend additional node capacity instead of a
cost reduction. The UI downloads this report as JSON and its manifests as a YAML bundle without a separate
download endpoint.
