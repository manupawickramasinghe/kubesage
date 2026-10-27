# Web API (`kubesage serve`)

JSON over HTTP on port 8600. Every POST must carry the header `X-KubeSage: 1`; without it the server answers
403. Browsers can't add that header cross-origin without a CORS preflight, which this server never answers, so
other websites can't trigger actions. There's no authentication beyond that.

| method | path | does |
|---|---|---|
| GET | `/` | the UI (`kubesage/web/static/index.html`) |
| GET | `/fonts/<file>.woff2` | vendored fonts, cached for a day; only files in `static/fonts` |
| GET | `/api/config` | `{namespace, mode: "live"\|"mock", tiers: {triage: model, …}, gitops: bool}` |
| GET | `/api/report` | the latest run: `{run, report}`, or 404 before the first analysis |
| GET | `/api/cluster` | live nodes, pods, scaling (see below), or `{available: false}` |
| POST | `/api/analyze` | runs the full pipeline and stores a run: `{run, report}`. 409 if one is already running; 502 for metrics or LLM errors. |
| POST | `/api/apply` | `{dry_run: bool}` → `{run, dry_run, steps: [{file, ok, skipped, output}]}` |
| POST | `/api/spike` | `{rps 1–200, work_ms 1–500, seconds 10–900, target?}` → `{job, TARGET, RPS, WORK_MS, SECONDS}` |
| POST | `/api/reset` | runs `scripts/demo-reset.sh` → `{ok, output: [last lines]}` |
| POST | `/api/gitops` | `{open_pr: bool}` → `{branch, commit, pushed, pr_url}`; 400 unless started with `--gitops-repo`/`--gitops-base` |

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
  "scaling": {"checkout-web": {"min": 1, "max": 3, "triggers": ["cron", "cpu"], "current": 3, "desired": 3, "cpu": 10}}
}
```

Unscheduled pods are listed under a pseudo-node named `(pending)` with role `pending`. `host`, `cpu_model` and
`os` come from the `kubesage.io/host`, `kubesage.io/cpu` and `kubesage.io/os` node annotations, which
`cluster-up.sh` and `join-node.sh` set.

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
