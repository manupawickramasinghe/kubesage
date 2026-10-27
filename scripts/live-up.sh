#!/usr/bin/env bash
# Stream live cluster metrics into the docker-compose VictoriaMetrics, after the 30-day backfill.
# Needs `docker compose up -d` and scripts/cluster-up.sh first. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="${KUBECONFIG:-$BIN_DIR/kubeconfig}"
VM_CONTAINER=${VM_CONTAINER:-kubesage-victoriametrics-1}
NETWORK="k3d-$CLUSTER_NAME"

docker inspect "$VM_CONTAINER" >/dev/null 2>&1 || { echo "start the metrics stack first: docker compose up -d" >&2; exit 1; }
# Two-machine clusters: the server's VXLAN checksum fix (see cluster-up.sh) is lost when its container restarts,
# e.g. after a reboot. Safe to repeat.
if [ -n "$(kubectl get nodes -l node-role.kubernetes.io/control-plane -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')" ]; then
  docker exec "k3d-$CLUSTER_NAME-server-0" ethtool -K flannel.1 tx-checksum-ip-generic off >/dev/null
fi
# Put VictoriaMetrics on the cluster's docker network and point vmagent at that IP. Pod DNS can't resolve
# docker container names, so use the IP directly.
if ! docker inspect -f '{{json .NetworkSettings.Networks}}' "$VM_CONTAINER" | grep -q "\"$NETWORK\""; then
  docker network connect "$NETWORK" "$VM_CONTAINER"
fi
VM_IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NETWORK\").IPAddress}}" "$VM_CONTAINER")

kubectl apply -f "$REPO_ROOT/deploy/k8s/monitoring/monitoring.yaml" >/dev/null
kubectl -n monitoring create configmap vmagent-remote --from-literal=url="http://$VM_IP:8428/api/v1/write" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n monitoring rollout restart deploy/vmagent >/dev/null
kubectl -n monitoring wait --for=condition=Available deploy --all --timeout=180s
echo "live metrics: vmagent -> http://$VM_IP:8428 (scrape every 15s). Spike the demo with: kubesage spike"
