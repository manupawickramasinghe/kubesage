#!/usr/bin/env bash
# Create the local k3d cluster, install KEDA and deploy the demo workloads in their "before" state.
# The kubeconfig is written to .bin/kubeconfig; ~/.kube/config is left alone.
#
# TWO_NODE=1 adds an agent node: the server node is labelled as the current (expensive) SKU and the agent
# as CHEAP_SKU, so `kubesage apply` can migrate pods onto the cheaper node live. Safe to re-run; it also
# upgrades an existing one-node cluster.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"
TWO_NODE=${TWO_NODE:-0}
CHEAP_SKU=${CHEAP_SKU:-F4s_v2}
"$(dirname "$0")/install-tools.sh" >/dev/null

read -r NODE_LABEL CURRENT_SKU < <("$PY" -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1]))
print(c["node_label"], c["current_pool"]["sku"])' "$REPO_ROOT/config/skus.yaml")

if ! k3d cluster list "$CLUSTER_NAME" >/dev/null 2>&1; then
  # Traefik is not needed; metrics-server (bundled with k3s) is, for KEDA's CPU trigger.
  k3d cluster create "$CLUSTER_NAME" --image "$K3S_IMAGE" --servers 1 --agents "$TWO_NODE" \
    --k3s-arg "--disable=traefik@server:0" \
    --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait
fi
k3d kubeconfig get "$CLUSTER_NAME" > "$KUBECONFIG"
chmod 600 "$KUBECONFIG"

agents() { kubectl get nodes -l '!node-role.kubernetes.io/control-plane' -o name; }
if [ "$TWO_NODE" = 1 ] && [ -z "$(agents)" ]; then
  k3d node create "$CLUSTER_NAME-agent" --cluster "$CLUSTER_NAME" --role agent --image "$K3S_IMAGE" --wait
  kubectl wait --for=condition=Ready nodes --all --timeout=120s
fi
kubectl label --overwrite $(kubectl get nodes -l node-role.kubernetes.io/control-plane -o name) "$NODE_LABEL=$CURRENT_SKU"
for node in $(agents); do kubectl label --overwrite "$node" "$NODE_LABEL=$CHEAP_SKU"; done

kubectl apply --server-side -f "https://github.com/kedacore/keda/releases/download/v$KEDA_VERSION/keda-$KEDA_VERSION.yaml" >/dev/null
kubectl -n keda wait --for=condition=Available deploy --all --timeout=240s
kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s

kubectl apply -k "$REPO_ROOT/deploy/k8s/demo/"
for d in $(kubectl -n demo get deploy -o name); do kubectl -n demo rollout status "$d" --timeout=180s >/dev/null; done
kubectl -n demo get deploy
kubectl get nodes -L "$NODE_LABEL"
echo
echo "export KUBECONFIG=$KUBECONFIG"
