#!/usr/bin/env bash
# Create the local k3d cluster, install KEDA and deploy the demo workloads in their "before" state.
# The kubeconfig is written to .bin/kubeconfig; ~/.kube/config is left alone.
#
# NODE_IP=<this machine's LAN IP> makes the cluster joinable by a second physical machine
# (scripts/join-node.sh): the API server, the kubelet and flannel's VXLAN port are published on that IP and it
# becomes the server's external IP. This is the real two-machine demo (docs/cluster.md).
#
# TWO_NODE=1 instead adds a second k3d container on this same host, labelled CHEAP_SKU, for a one-machine demo.
# Either way the server node is labelled as the current (expensive) pool so `kubesage apply` can migrate pods
# onto the cheaper node live. Safe to re-run; it also upgrades an existing one-node cluster.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"
TWO_NODE=${TWO_NODE:-0}
NODE_IP=${NODE_IP:-}
CHEAP_SKU=${CHEAP_SKU:-F4s_v2}
"$(dirname "$0")/install-tools.sh" >/dev/null

read -r NODE_LABEL CURRENT_SKU < <("$PY" -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1]))
print(c["node_label"], c["current_pool"]["sku"])' "$REPO_ROOT/config/skus.yaml")

if ! k3d cluster list "$CLUSTER_NAME" >/dev/null 2>&1; then
  lan=()
  if [ -n "$NODE_IP" ]; then
    # Docker-published ports, so the other machine can reach the API server, this node's kubelet and VXLAN.
    # The external IP becomes the advertised API address and flannel's tunnel endpoint for this node.
    lan=(--api-port "$NODE_IP:6443" -p "$NODE_IP:8472:8472/udp@server:0:direct" -p "$NODE_IP:10250:10250/tcp@server:0:direct"
         --k3s-arg "--node-external-ip=$NODE_IP@server:0" --k3s-arg "--flannel-external-ip@server:0")
  fi
  # Traefik is not needed; metrics-server (bundled with k3s) is, for KEDA's CPU trigger.
  k3d cluster create "$CLUSTER_NAME" --image "$K3S_IMAGE" --servers 1 --agents "$TWO_NODE" \
    --k3s-arg "--disable=traefik@server:0" "${lan[@]}" \
    --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait
fi
k3d kubeconfig get "$CLUSTER_NAME" > "$KUBECONFIG"
if [ -n "$NODE_IP" ]; then
  # VXLAN through Docker's NAT arrives with bad TCP/UDP checksums while the kernel offloads them (ping still
  # works, which makes it confusing). Computing them in software fixes cross-machine pod traffic. flannel.1 is
  # recreated with the container, so this runs on every cluster-up.
  docker exec "k3d-$CLUSTER_NAME-server-0" ethtool -K flannel.1 tx-checksum-ip-generic off >/dev/null
fi
chmod 600 "$KUBECONFIG"

agents() { kubectl get nodes -l '!node-role.kubernetes.io/control-plane' -o name; }
if [ "$TWO_NODE" = 1 ] && [ -z "$(agents)" ]; then
  k3d node create "$CLUSTER_NAME-agent" --cluster "$CLUSTER_NAME" --role agent --image "$K3S_IMAGE" --wait
  kubectl wait --for=condition=Ready nodes --all --timeout=120s
fi
server=$(kubectl get nodes -l node-role.kubernetes.io/control-plane -o name)
kubectl label --overwrite $server "$NODE_LABEL=$CURRENT_SKU" kubesage.io/pool=system
for node in $(agents); do
  [ -n "$(kubectl get "$node" -o jsonpath='{.metadata.annotations.kubesage\.io/host}')" ] && continue  # a joined machine
  kubectl label --overwrite "$node" "$NODE_LABEL=$CHEAP_SKU" kubesage.io/pool=lab
done
# What the UI shows for each node: the real machine behind it, not just the pool label.
for node in $server $( [ "$TWO_NODE" = 1 ] && agents || true ); do
  kubectl annotate --overwrite "$node" "kubesage.io/host=$(hostname)" \
    "kubesage.io/cpu=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//')" \
    "kubesage.io/os=$(. /etc/os-release && echo "$PRETTY_NAME")" >/dev/null
done

kubectl apply --server-side -f "https://github.com/kedacore/keda/releases/download/v$KEDA_VERSION/keda-$KEDA_VERSION.yaml" >/dev/null
# Cluster infrastructure stays on the server, so it keeps working while a joined laptop is asleep.
pin='{"spec":{"template":{"spec":{"nodeSelector":{"node-role.kubernetes.io/control-plane":"true"}}}}}'
for d in $(kubectl -n keda get deploy -o name); do kubectl -n keda patch "$d" -p "$pin" >/dev/null; done
for d in metrics-server coredns; do kubectl -n kube-system patch deploy "$d" -p "$pin" >/dev/null; done
kubectl -n keda wait --for=condition=Available deploy --all --timeout=240s
kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s

kubectl apply -k "$REPO_ROOT/deploy/k8s/demo/"
for d in $(kubectl -n demo get deploy -o name); do kubectl -n demo rollout status "$d" --timeout=180s >/dev/null; done
kubectl -n demo get deploy
kubectl get nodes -L "$NODE_LABEL"
echo
echo "export KUBECONFIG=$KUBECONFIG"
