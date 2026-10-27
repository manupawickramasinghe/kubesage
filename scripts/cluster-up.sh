#!/usr/bin/env bash
# Create the local k3d cluster, install KEDA and deploy the demo workloads in their "before" state.
# The kubeconfig is written to .bin/kubeconfig; ~/.kube/config is left alone.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"
"$(dirname "$0")/install-tools.sh" >/dev/null

if ! k3d cluster list "$CLUSTER_NAME" >/dev/null 2>&1; then
  # Traefik is not needed; metrics-server (bundled with k3s) is, for KEDA's CPU trigger.
  k3d cluster create "$CLUSTER_NAME" --image "$K3S_IMAGE" --servers 1 --agents 0 \
    --k3s-arg "--disable=traefik@server:0" \
    --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait
fi
k3d kubeconfig get "$CLUSTER_NAME" > "$KUBECONFIG"
chmod 600 "$KUBECONFIG"

kubectl apply --server-side -f "https://github.com/kedacore/keda/releases/download/v$KEDA_VERSION/keda-$KEDA_VERSION.yaml" >/dev/null
kubectl -n keda wait --for=condition=Available deploy --all --timeout=240s
kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s

kubectl apply -f "$REPO_ROOT/deploy/k8s/demo/"
kubectl -n demo wait --for=condition=Available deploy --all --timeout=180s
kubectl -n demo get deploy
echo
echo "export KUBECONFIG=$KUBECONFIG"
