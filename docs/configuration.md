# Configuration

## Files

| file | what it sets |
|---|---|
| `config/models.yaml` | default LLM profile (Meta Model API): base URL, key env var, tiers with model, label, prices, max tokens |
| `config/models.<name>.yaml` | other profiles, picked with `KUBESAGE_MODELS=<name>`. `cerebras` ships with the repo. |
| `config/skus.yaml` | the cost model: SKU catalog with Azure-like list prices, reference SKU for unit prices, allocatable fractions, `min_nodes`, the "before" pool, and the node label used for pools |
| `scripts/versions.sh` | pinned k3d, kubectl, k3s image and KEDA versions, and the cluster name |
| `deploy/k8s/demo/` | the demo workloads and their "before" requests (kustomize) |
| `.env` | API keys. **Gitignored; never commit it.** |

### A model tier

```yaml
tiers:
  worker:
    label: OSS medium                 # shown in the UI and reports
    model: gpt-oss-120b
    input_per_mtok: 0.35              # USD per 1M input tokens
    cached_input_per_mtok: 0.35       # optional
    output_per_mtok: 0.75
    max_output_tokens: 6000
    extra_body: {reasoning_effort: medium}   # passed through to the provider
```

The three tier keys are fixed: `triage`, `worker`, `strategist`. Any OpenAI-compatible `/chat/completions`
endpoint works. The client asks for `response_format: json_object`, retries 429/5xx up to 4 times (honouring
`Retry-After`), and retries a schema-invalid answer once.

### The cost model

```yaml
pricing_reference: D4s_v5       # unit prices per core-hour and GiB-hour come from this SKU
cpu_to_gib_cost_ratio: 7.5      # 1 vCPU costs as much as 7.5 GiB
allocatable_cpu: 0.90           # share of a node pods can request
allocatable_mem: 0.80
min_nodes: 1                    # the demo runs on one worker at a time
current_pool: {sku: D8s_v5, nodes: 1}   # the "before" pool: the 8600G desktop
node_label: kubesage.io/sku     # migrations target nodes with this label = chosen SKU
```

Real machines are mapped to pools by that label: the desktop is `D8s_v5`, set by `cluster-up.sh` from
`current_pool`. The laptop is `F8s_v2`, the default in `join-node.sh`, being the closest catalog SKU to 8
threads and 14 GiB.

## Environment variables

| variable | used by | meaning |
|---|---|---|
| `CEREBRAS_API_KEY` | Cerebras profile | API key (keep it in `.env`) |
| `MODEL_API_KEY` | default Meta profile | API key |
| `KUBESAGE_MODELS` | analyze, serve | profile name: loads `config/models.<name>.yaml` |
| `KUBESAGE_VM_URL` | all metric commands | Prometheus/VictoriaMetrics URL, default `http://localhost:8428` |
| `KUBESAGE_TZ` | all metric commands | timezone for daily patterns and cron triggers, default `Asia/Colombo` |
| `KUBESAGE_CONFIG_DIR` | all | alternative config directory |
| `KUBECONFIG` | apply, serve, scripts | default `.bin/kubeconfig` |
| `NODE_IP` | `cluster-up.sh` | make the cluster joinable from the LAN on this IP (see [cluster.md](cluster.md)) |
| `TWO_NODE`, `CHEAP_SKU` | `cluster-up.sh` | single-host alternative: a second k3d container labelled `CHEAP_SKU` |
| `SUDO_PASSWORD` | `join-node.sh`, `leave-node.sh` | remote sudo password. Prompted for when unset; sent over SSH stdin only. |
| `VM_CONTAINER` | `live-up.sh` | VictoriaMetrics container name, default `kubesage-victoriametrics-1` |

## CLI flags shared by `stats`, `analyze`, `serve`, `simulate`

`--vm`, `--namespace` (default `demo`), `--tz`, `--days` (default 30). `analyze`/`serve` also take
`--mode auto|mock|live` (auto means live when the profile's key variable is set) and `--out-dir` (default `out`).
`serve` adds `--host`, `--port` (8600), `--gitops-repo`, `--gitops-base`, `--gitops-path`.

## Secrets

- API keys live only in `.env` (mode 600, gitignored). The repo is public.
- The laptop's sudo password is never stored. `join-node.sh` prompts for it, or reads it from the environment
  for one command.
- The k3s join token stays on the desktop (inside the k3d container) and in the laptop's root-only
  `/etc/systemd/system/k3s-agent.service.env`.
