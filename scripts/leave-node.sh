#!/usr/bin/env bash
# Remove a machine added by scripts/join-node.sh: drain and delete its node (if the cluster is still up), then on
# the machine uninstall the k3s agent and remove the VXLAN udev rule and the "kubesage" ufw rules. Safe to repeat,
# and works after the cluster is already gone.   scripts/leave-node.sh manupa@192.168.1.92
# Asks for the machine's sudo password unless SUDO_PASSWORD is set; it travels over SSH stdin only.
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"
REMOTE=${1:?usage: leave-node.sh user@host}

NAME=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" hostname) || { echo "can't reach $REMOTE over SSH" >&2; exit 1; }
if [ -f "$KUBECONFIG" ] && kubectl get "node/$NAME" --request-timeout=5s >/dev/null 2>&1; then
  kubectl drain "node/$NAME" --ignore-daemonsets --delete-emptydir-data --timeout=120s >/dev/null || true
  kubectl delete "node/$NAME" --ignore-not-found
fi
if [ -z "${SUDO_PASSWORD:-}" ]; then read -rsp "sudo password on $REMOTE: " SUDO_PASSWORD; echo; fi

# sudo -S takes the first line of stdin; sh -s runs the rest.
{ printf '%s\n' "$SUDO_PASSWORD"; cat <<'REMOTE_SCRIPT'
set -u
# k3s' own uninstaller: stops the agent, removes its interfaces and only its iptables chains (Docker's stay),
# the binary, the systemd unit and /var/lib/rancher.
[ -x /usr/local/bin/k3s-agent-uninstall.sh ] && /usr/local/bin/k3s-agent-uninstall.sh >/dev/null 2>&1
rm -f /etc/udev/rules.d/90-kubesage-flannel.rules
if command -v ufw >/dev/null; then
  # join-node.sh comments its rules "kubesage ..."; delete from the bottom so the numbers stay valid.
  for n in $(ufw status numbered 2>/dev/null | grep kubesage | sed -n 's/^\[ *\([0-9]*\)\].*/\1/p' | sort -rn); do
    ufw --force delete "$n" >/dev/null
  done
fi
echo "removed the k3s agent from $(hostname)"
REMOTE_SCRIPT
} | ssh "$REMOTE" 'sudo -S -p "" sh -s'

if [ -f "$BIN_DIR/joined-nodes" ]; then
  grep -vxF "$REMOTE" "$BIN_DIR/joined-nodes" > "$BIN_DIR/joined-nodes.tmp" || true
  mv "$BIN_DIR/joined-nodes.tmp" "$BIN_DIR/joined-nodes"
fi
