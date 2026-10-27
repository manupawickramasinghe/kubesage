#!/usr/bin/env bash
# Remove a machine added by scripts/join-node.sh: drain it, delete the node, uninstall the k3s agent there and
# drop its firewall rules.   scripts/leave-node.sh manupa@192.168.1.92
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"
REMOTE=${1:?usage: leave-node.sh user@host}
NAME=$(ssh "$REMOTE" hostname)
SERVER_IP=$(kubectl get nodes -l node-role.kubernetes.io/control-plane \
  -o jsonpath='{.items[0].status.addresses[?(@.type=="ExternalIP")].address}')
kubectl drain "node/$NAME" --ignore-daemonsets --delete-emptydir-data --timeout=120s || true
kubectl delete "node/$NAME" --ignore-not-found
if [ -z "${SUDO_PASSWORD:-}" ]; then read -rsp "sudo password on $REMOTE: " SUDO_PASSWORD; echo; fi
{ printf '%s\n' "$SUDO_PASSWORD"; cat <<REMOTE_SCRIPT
[ -x /usr/local/bin/k3s-agent-uninstall.sh ] && /usr/local/bin/k3s-agent-uninstall.sh
rm -f /etc/udev/rules.d/90-kubesage-flannel.rules
if command -v ufw >/dev/null; then
  ufw delete allow from $SERVER_IP to any port 10250 proto tcp >/dev/null 2>&1 || true
  ufw delete allow from $SERVER_IP to any port 8472 proto udp >/dev/null 2>&1 || true
fi
REMOTE_SCRIPT
} | ssh "$REMOTE" 'sudo -S -p "" sh -s'
