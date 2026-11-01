# Running KubeSage in your cluster, with Flux

Until now KubeSage ran on an operator's machine, outside the cluster. This is the
in-cluster shape: KubeSage as a container, deployed by Flux, with **read-only**
cluster access. All changes still flow through git — the pod can look and propose,
never touch.

## What runs where

| piece | kind | what it does |
|---|---|---|
| Control Center | `Deployment` + `Service` (port 8600) | The dashboard. Analyze writes reports to a small PVC (`/app/out`). Apply buttons cannot write to the cluster: the ServiceAccount has no write verbs, by design. |
| Scheduled analysis | `CronJob` (Mon 06:15, edit freely) | Clones your GitOps repo with a deploy key, runs `kubesage analyze --mode live`, then `kubesage gitops --push`: a `kubesage/rightsize-<run>` branch with the kustomize overlay + `REPORT.md`. |
| Flux | your existing install | Reconciles KubeSage itself from `deploy/k8s/kubesage`, and reconciles your workloads from the overlay path in your GitOps repo — **after a human merges the KubeSage branch/PR**. Merging is the apply. |

Set `API_TOKEN` in that Secret: the UI Deployment reads it as `KUBESAGE_API_TOKEN`, and every
mutating POST then requires it (the dashboard prompts once per tab and keeps it in
`sessionStorage`). Without it, anything that can reach the Service could press Apply —
the token is what makes the in-cluster UI safe to expose beyond loopback.

Why git-only writes? The pod holds an LLM API key and runs model-influenced code.
Read-only RBAC (`get/list/watch` on pods, nodes, deployments, jobs, KEDA
ScaledObjects) means a compromised or confused pod can leak a report, not resize
your cluster. Flux + a human merge remain the only write path, exactly as on a
laptop — the Executor's PR model, generalized.

## Install

```bash
# 1. Build and push YOUR image (the manifest default is a placeholder registry)
docker build -t ghcr.io/YOU/kubesage:latest . && docker push ghcr.io/YOU/kubesage:latest

# 2. Create secrets imperatively — never commit filled copies
kubectl create namespace kubesage
kubectl -n kubesage create secret generic kubesage-secrets --from-literal=MODEL_API_KEY='LLM|...'
ssh-keygen -t ed25519 -f ~/.ssh/kubesage-executor -N ""   # add the .pub as a write deploy key on your GitOps repo
kubectl -n kubesage create secret generic kubesage-git-key --from-file=ssh-privatekey=~/.ssh/kubesage-executor

# 3. Edit the EDIT markers (image, VM URL, timezone, target namespace, GitOps repo URL/base)
#    in deploy/k8s/kubesage/{kustomization,deployment,cronjob}.yaml, then either:
kubectl apply -k deploy/k8s/kubesage
# or let Flux do it: kubectl apply -f deploy/k8s/kubesage/flux/  (after pointing it at your fork)
```

Your metrics must be reachable from the pod: set `KUBESAGE_VM_URL` to your
Prometheus/VictoriaMetrics Service DNS. Your own prices and calendar travel either
in your image (rebuild with edited `config/`) or in a `kubesage-config` ConfigMap
mounted over `/app/config` — see [configmap.example.yaml](../deploy/k8s/kubesage/configmap.example.yaml).

## The weekly loop

1. CronJob fires → clones the GitOps repo → analyzes against live metrics.
2. A branch lands with patches, ScaledObjects and the savings report.
3. You review the diff (guardrail notes say exactly what was clamped and why).
4. Merge → Flux reconciles → KEDA and the Deployments do the scaling at runtime.

No merge, no change. To stop KubeSage entirely, suspend the CronJob; the UI
Deployment is stateless apart from its reports PVC.

## Limits, honestly

- The in-cluster UI's live-cluster views need the read RBAC above; the agent-team
  and Apply actions that write are laptop/GitOps flows, not pod flows.
- CronJob analysis costs one live Analyze per run (Meta Muse: ~\$0.27 at the
  prices in `config/models.yaml`, measured Nov 2026).
- One-off dated events (Black Friday) still use the timed CronJobs KubeSage
  renders — Flux applies them like any other manifest in the overlay.
