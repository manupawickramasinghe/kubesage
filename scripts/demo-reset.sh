#!/usr/bin/env bash
# Put the demo namespace back into its oversized "before" state so the demo can be run again.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="${KUBECONFIG:-$BIN_DIR/kubeconfig}"

kubectl -n demo delete scaledobjects.keda.sh -l app.kubernetes.io/managed-by=kubesage --ignore-not-found
# `kubectl apply` would keep fields that `kubectl patch` added, so replace the objects outright.
kubectl replace -f "$REPO_ROOT/deploy/k8s/demo/" || kubectl apply -f "$REPO_ROOT/deploy/k8s/demo/"
kubectl -n demo wait --for=condition=Available deploy --all --timeout=180s
kubectl -n demo get deploy
