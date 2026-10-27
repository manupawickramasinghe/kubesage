#!/usr/bin/env bash
# Put the demo namespace back into its oversized "before" state so the demo can be run again.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="${KUBECONFIG:-$BIN_DIR/kubeconfig}"

kubectl -n demo delete scaledobjects.keda.sh -l app.kubernetes.io/managed-by=kubesage --ignore-not-found
kubectl -n demo delete jobs -l app.kubernetes.io/managed-by=kubesage --ignore-not-found
kubectl apply -k "$REPO_ROOT/deploy/k8s/demo/" >/dev/null
# `kubectl apply` would keep fields that `kubectl patch` added, so replace the Deployments outright.
kubectl kustomize "$REPO_ROOT/deploy/k8s/demo/" \
  | "$PY" -c 'import sys, yaml; print(yaml.safe_dump_all(d for d in yaml.safe_load_all(sys.stdin) if d["kind"] == "Deployment"))' \
  | kubectl replace --save-config -f -
for d in $(kubectl -n demo get deploy -o name); do kubectl -n demo rollout status "$d" --timeout=180s >/dev/null; done
kubectl -n demo get deploy
