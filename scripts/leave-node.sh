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

# sudo -S takes the first line of stdin; sh -s runs the rest. The remote part stops at the first failure and says
# so; the record in .bin/joined-nodes is only removed once the machine is verifiably clean.
{ printf '%s\n' "$SUDO_PASSWORD"; cat <<'REMOTE_SCRIPT'
set -eu
fail() { echo "leave-node: $*" >&2; exit 1; }
# k3s' own uninstaller: stops the agent, removes its interfaces and only its iptables chains (Docker's stay),
# the binary, the systemd unit and /var/lib/rancher.
if [ -x /usr/local/bin/k3s-agent-uninstall.sh ]; then
  log=$(mktemp)
  /usr/local/bin/k3s-agent-uninstall.sh >"$log" 2>&1 || { tail -n 20 "$log" >&2; rm -f "$log"; fail "k3s-agent-uninstall.sh failed"; }
  rm -f "$log"
elif [ -e /usr/local/bin/k3s ]; then
  fail "k3s is installed but /usr/local/bin/k3s-agent-uninstall.sh is missing; remove it by hand"
fi
rm -f /etc/udev/rules.d/90-kubesage-flannel.rules
if command -v ufw >/dev/null; then
  # join-node.sh comments its rules "kubesage ..."; delete from the bottom so the numbers stay valid.
  for n in $(ufw status numbered 2>/dev/null | grep kubesage | sed -n 's/^\[ *\([0-9]*\)\].*/\1/p' | sort -rn); do
    ufw --force delete "$n" >/dev/null || fail "ufw delete $n failed"
  done
fi
left=""
for f in /usr/local/bin/k3s /etc/systemd/system/k3s-agent.service /var/lib/rancher/k3s /etc/udev/rules.d/90-kubesage-flannel.rules; do
  if [ -e "$f" ]; then left="$left $f"; fi
done
[ -z "$left" ] || fail "still there after the uninstall:$left"
echo "removed the k3s agent from $(hostname)"
REMOTE_SCRIPT
} | ssh "$REMOTE" 'sudo -S -p "" sh -s' || { echo "$REMOTE was not cleaned; .bin/joined-nodes still lists it" >&2; exit 1; }

if [ -f "$BIN_DIR/joined-nodes" ]; then
  grep -vxF "$REMOTE" "$BIN_DIR/joined-nodes" > "$BIN_DIR/joined-nodes.tmp" || true
  mv "$BIN_DIR/joined-nodes.tmp" "$BIN_DIR/joined-nodes"
fi
