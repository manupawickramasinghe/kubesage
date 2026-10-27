#!/usr/bin/env bash
# Join a second physical machine to the cluster as a k3s agent, over SSH.
#   NODE_IP=<this machine's LAN IP> scripts/cluster-up.sh     # first, once
#   scripts/join-node.sh manupa@192.168.1.92 [pool-label]      # default F8s_v2: the SKU closest to a 3500U (8 threads, 14 GiB)
# Needs key-based SSH to the machine. Asks for its sudo password unless SUDO_PASSWORD is set; the password and
# the join token travel over SSH stdin, never on a command line. Undo with scripts/leave-node.sh.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"
REMOTE=${1:?usage: join-node.sh user@host [pool-label]}
POOL=${2:-F8s_v2}

NODE_LABEL=$("$PY" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["node_label"])' "$REPO_ROOT/config/skus.yaml")
SERVER_IP=$(kubectl get nodes -l node-role.kubernetes.io/control-plane \
  -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')
[ -n "$SERVER_IP" ] || { echo "the server has no external IP: recreate it with NODE_IP=<lan-ip> scripts/cluster-up.sh" >&2; exit 1; }
TOKEN=$(docker exec "k3d-$CLUSTER_NAME-server-0" cat /var/lib/rancher/k3s/server/node-token)
read -r NAME HOST_IP CPU < <(ssh "$REMOTE" "printf '%s %s %s\n' \"\$(hostname)\" \
  \"\$(ip -4 route get $SERVER_IP | grep -o 'src [0-9.]*' | cut -d' ' -f2)\" \"\$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//')\"")
K3S_VERSION=${K3S_IMAGE#*:}; K3S_VERSION=${K3S_VERSION/-k3s/+k3s}  # rancher/k3s:v1.36.4-k3s1 -> v1.36.4+k3s1
echo "joining $NAME ($HOST_IP, $CPU) to https://$SERVER_IP:6443 with k3s $K3S_VERSION"
if [ -z "${SUDO_PASSWORD:-}" ]; then read -rsp "sudo password on $REMOTE: " SUDO_PASSWORD; echo; fi

# sudo -S takes the first line of stdin; sh -s runs the rest.
{ printf '%s\n' "$SUDO_PASSWORD"; cat <<REMOTE_SCRIPT
set -eu
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  # Only the server may reach this machine's kubelet (logs, metrics) and VXLAN tunnel.
  ufw allow from $SERVER_IP to any port 10250 proto tcp comment 'kubesage kubelet' >/dev/null
  ufw allow from $SERVER_IP to any port 8472 proto udp comment 'kubesage flannel vxlan' >/dev/null
fi
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='$K3S_VERSION' K3S_URL='https://$SERVER_IP:6443' K3S_TOKEN='$TOKEN' \
  sh -s - agent --node-ip $HOST_IP --node-external-ip $HOST_IP
# Same VXLAN checksum fix as the server side in cluster-up.sh, kept across reboots by a udev rule.
cat > /etc/udev/rules.d/90-kubesage-flannel.rules <<'RULE'
ACTION=="add", SUBSYSTEM=="net", KERNEL=="flannel.1", RUN+="/usr/sbin/ethtool -K flannel.1 tx-checksum-ip-generic off"
RULE
for _ in \$(seq 30); do ip link show flannel.1 >/dev/null 2>&1 && break; sleep 1; done
ethtool -K flannel.1 tx-checksum-ip-generic off >/dev/null
REMOTE_SCRIPT
} | ssh "$REMOTE" 'sudo -S -p "" sh -s'

docker exec "k3d-$CLUSTER_NAME-server-0" ethtool -K flannel.1 tx-checksum-ip-generic off >/dev/null
for _ in $(seq 60); do kubectl get node "$NAME" >/dev/null 2>&1 && break; sleep 2; done
kubectl wait --for=condition=Ready "node/$NAME" --timeout=180s
kubectl label --overwrite "node/$NAME" "$NODE_LABEL=$POOL" >/dev/null
kubectl annotate --overwrite "node/$NAME" "kubesage.io/host=$NAME" "kubesage.io/cpu=$CPU" \
  "kubesage.io/os=$(ssh "$REMOTE" '. /etc/os-release && echo "$PRETTY_NAME"')" >/dev/null
kubectl get nodes -L "$NODE_LABEL" -o wide
