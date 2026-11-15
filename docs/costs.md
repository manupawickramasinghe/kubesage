# Cost allocation

`kubesage analyze` right-sizes one namespace. `kubesage costs` answers the question that comes first: **where
does the whole cluster's bill go?** It reads every namespace from the same Prometheus or VictoriaMetrics, calls no
model, and costs nothing to run.

```bash
kubesage simulate                 # 30 days of a small shop's cluster: 6 namespaces, 2 nodes, 3 teams
kubesage costs                    # by namespace
kubesage costs --by workload      # or --by team
kubesage budgets                  # exits 1 when a budget is over
kubesage serve                    # the same report in the Costs view, and on /metrics
```

## How a pod is billed

KubeSage follows the [OpenCost](https://www.opencost.io/) convention, so the numbers can be checked against it.

1. **The node bill.** Each node costs its SKU's hourly list price from `config/skus.yaml` for every hour it
   existed. That price is split between CPU and memory by `cpu_to_gib_cost_ratio`, which gives the cluster a
   blended price per core-hour and per GiB-hour for each day.
2. **A pod pays for the larger of what it requested and what it used**, per day, for CPU and for memory. A pod
   that requests 2 cores and uses 0.1 is billed 2 cores: the scheduler held them for it.
3. **What is left is idle.** Node cost that no pod was billed for is shown on its own, per resource: spare memory
   does not make up for CPU that is requested beyond capacity.
4. **Volumes** are billed per provisioned GiB-month (`storage_usd_per_gib_month`).

Pods roll up to the controller that owns them (ReplicaSet → Deployment, Job → CronJob, StatefulSet, DaemonSet),
then to their namespace and their team. Without owner metrics the pod name is read instead, and the kind
`Workload` says that it was a guess.

| column | meaning |
|---|---|
| **Per month** | Direct cost: the last seven observed days as a monthly run rate |
| **With shared** | Direct cost plus a proportional part of the shared namespaces and of idle capacity. This column adds up to the whole bill, so it is the one to charge back |
| **Share** | That loaded cost as a percentage of the cluster |
| **Efficiency** | Cost of what was used ÷ cost of what was billed |

## Findings

Each finding names a target, says what was measured and what to do. Savings are summed into "savings to check";
risks and notes are listed without a saving.

| finding | when | counted as a saving |
|---|---|---|
| **Abandoned workload** | Used under 1.5% of its requested CPU over the last seven days, has run for at least three days and is still running | yes: its whole cost |
| **Over-requested** | Uses under 40% of what it is billed for | yes: an estimate that keeps 1.5× its average CPU and 1.25× its average memory. `kubesage analyze` gives the real plan, checked against peaks |
| **Unused volume** | A claim no pod mounted during the window | yes: its storage cost |
| **No requests set** | Uses CPU or memory but requests none | no: a risk |
| **Cost spike** | A namespace's last day is 30% and $0.50 above its usual day; the workload behind most of the rise is named | no: what it costs per month if it stays |
| **Idle capacity** | 35% or more of the node bill is idle | no |

A saving on a request only reaches the bill when nodes are removed. That is what the node-pool plan in
`kubesage analyze` is for, and why idle capacity is listed beside the savings instead of added to them.

## Teams, shared costs and budgets: `config/costs.yaml`

```yaml
team_label: team                      # the pod or namespace label that names the owner
namespace_teams: {demo: storefront}   # fallback when the label is absent
shared_namespaces: [kube-system, monitoring]
storage_usd_per_gib_month: 0.08
budgets:
  - {scope: cluster, monthly_usd: 600}
  - {scope: team, name: storefront, monthly_usd: 260}
  - {scope: namespace, name: staging, monthly_usd: 30}
budget_warn_ratio: 0.8
```

A pod's team is its own label, then its namespace's label, then `namespace_teams`, then `unassigned`. A budget is
compared with the direct run rate: `ok`, `at-risk` from 80%, `over` above 100%. `kubesage budgets` exits 1 when
one is over, so a pipeline or a cron job can act on it. Every key is optional; without the file there are no
teams and no budgets, and only `kube-system` is shared.

## What the cluster must export

The queries use the standard cAdvisor and kube-state-metrics names:

| needed for | series |
|---|---|
| usage and requests (required) | `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `kube_pod_container_resource_requests` |
| controllers | `kube_pod_owner`, `kube_replicaset_owner`, `kube_job_owner` |
| teams | `kube_pod_labels`, `kube_namespace_labels` |
| node prices | `kube_node_info`, `kube_node_labels`, `kube_node_status_capacity` |
| volumes | `kube_persistentvolumeclaim_resource_requests_storage_bytes`, `kube_pod_spec_volumes_persistentvolumeclaims_info` |

kube-state-metrics only exports the labels it is told to. Start it with:

```
--metric-labels-allowlist=nodes=[node.kubernetes.io/instance-type],pods=[team],namespaces=[team]
```

`deploy/k8s/monitoring/` already does, and keeps these series in its scrape. Nothing but usage and requests is
required: what is missing degrades with a note in the report.

| missing | what happens |
|---|---|
| owner metrics | workloads are read from pod names |
| team labels | `namespace_teams`, then `unassigned` |
| a node's SKU label | the node is matched to a catalog SKU by its size, or priced per core and GiB |
| all node metrics | the cluster is priced as `current_pool` from `config/skus.yaml` |

## API and metrics

`GET /api/costs?days=30` returns `{report, config}`; `days` is 1 to 90 and a report is cached for a minute.
`report` has `cluster`, `nodes`, `by_namespace`, `by_workload`, `by_team`, `budgets`, `findings`,
`potential_monthly_usd`, `days` (the date of each daily value) and `notes`. `kubesage costs --json` prints the
same object.

`GET /metrics` serves the report as Prometheus text, so Grafana can chart it and Alertmanager can page on it:

```
kubesage_cluster_monthly_cost_usd 582.24
kubesage_cluster_idle_monthly_cost_usd 268.11
kubesage_potential_monthly_savings_usd 170.44
kubesage_monthly_cost_usd{scope="namespace",name="demo"} 169.25
kubesage_efficiency_ratio{namespace="demo"} 0.18
kubesage_budget_used_ratio{scope="namespace",name="staging"} 1.1077
```

```yaml
- alert: KubeSageBudgetOver
  expr: kubesage_budget_used_ratio > 1
  for: 6h
  annotations: {summary: "{{ $labels.scope }} {{ $labels.name }} is over its monthly budget"}
```

## Limits

- **List prices.** Prices come from the catalog in `config/skus.yaml`, which you edit. Nothing is reconciled with
  a cloud invoice: no reserved-instance, savings-plan or spot discounts, no negotiated rates.
- **CPU, memory and volumes only.** Network transfer, load balancers, GPUs and managed services are not billed.
- **One cluster.** There is no view across clusters.
- **Rolling days.** A day is the 24 hours ending at the time of the query, not a calendar day, and the monthly
  figure is a run rate, not a month-to-date total.
- **Requests count while a pod exists**, including a pod that is Pending or has finished and not been deleted.
- **Verified on simulated data.** The arithmetic is tested against hand-made clusters and the queries against a
  real VictoriaMetrics with simulated history. It has not yet run on a live cluster with real owner and label
  metrics.
