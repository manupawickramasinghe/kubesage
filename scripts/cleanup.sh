#!/usr/bin/env bash
# Tear KubeSage down, on this machine and on every machine joined with scripts/join-node.sh.
#
#   scripts/cleanup.sh --check     report what is still installed or running; changes nothing
#   scripts/cleanup.sh --dry-run   list what would be removed; changes nothing
#   scripts/cleanup.sh             remove the clusters: stop `kubesage serve`, uninstall k3s on joined machines,
#                                  delete the k3d cluster and its kubeconfig. Keeps the metrics history, tools,
#                                  images and analysis runs, so a rebuild is quick.
#   scripts/cleanup.sh --all       also VictoriaMetrics + Grafana with all their data, the k3s/k3d/metrics
#                                  images, .bin/ and out/
#
#   --yes             don't ask for confirmation
#   --skip-nodes      leave joined machines alone
#   --node user@host  also clean this machine (repeatable). Joined machines are found in .bin/joined-nodes and,
#                     while the cluster is up, from its non-k3d nodes (as $SSH_USER@ip, default $USER)
#
# Never touches .env, .venv, the repo, or containers, images and networks KubeSage didn't create. Joined machines
# need their sudo password: asked once, or set SUDO_PASSWORD. Exit status 0 means nothing was left behind.
# Details: docs/cleanup.md
set -euo pipefail
source "$(dirname "$0")/versions.sh"
export KUBECONFIG="$BIN_DIR/kubeconfig"

MODE=clean ALL=0 YES=0 SKIP_NODES=0 NODES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --dry-run) MODE=dry-run ;;
    --all) ALL=1 ;;
    --yes|-y) YES=1 ;;
    --skip-nodes) SKIP_NODES=1 ;;
    --node) NODES+=("${2:?--node needs user@host}"); shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ "$SKIP_NODES" = 1 ]; then
  NODES=()
else
  [ -f "$BIN_DIR/joined-nodes" ] && while read -r n; do NODES+=("$n"); done < "$BIN_DIR/joined-nodes"
  if [ -f "$KUBECONFIG" ] && command -v kubectl >/dev/null; then
    while read -r ip; do NODES+=("${SSH_USER:-$USER}@$ip"); done < <(kubectl get nodes --request-timeout=5s \
      -o jsonpath='{range .items[*]}{.metadata.name} {.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' \
      2>/dev/null | awk '$1 !~ /^k3d-/ && NF == 2 {print $2}')
  fi
fi
# One entry per host; the first spelling wins, so .bin/joined-nodes beats a guessed user.
mapfile -t NODES < <(printf '%s\n' "${NODES[@]}" | awk -F@ 'NF && !seen[$NF]++')

compose() { docker compose -f "$REPO_ROOT/docker-compose.yml" "$@"; }
COMPOSE_PROJECT=$(awk '/^name:/ {print $2; exit}' "$REPO_ROOT/docker-compose.yml")
ui_pids() { pgrep -f '[k]ubesage(\.cli)? serve' || true; }  # [k]: the pattern must not match its own command line
k3d_containers() { docker ps -a --filter "label=k3d.cluster=$CLUSTER_NAME" --format '{{.Names}}'; }
k3d_leftovers() {
  docker network ls -q --filter "name=^k3d-$CLUSTER_NAME\$"
  docker volume ls -q --filter "label=k3d.cluster=$CLUSTER_NAME"
}
own_kubeconfig() { [ -f "$KUBECONFIG" ] && grep -q "^current-context: k3d-$CLUSTER_NAME\$" "$KUBECONFIG"; }
# Containers on the cluster network besides k3d's own: "ours <name>" for the compose stack live-up.sh attached,
# "foreign <name>" for anything else, which cleanup never detaches.
attached() {
  local c owner
  for c in $(docker ps -aq --filter "network=k3d-$CLUSTER_NAME"); do
    owner=$(docker inspect -f '{{index .Config.Labels "k3d.cluster"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{.Name}}' "$c")
    case "$owner" in
      "$CLUSTER_NAME|"*) ;;
      "|$COMPOSE_PROJECT|"*) echo "ours ${owner##*|/}" ;;
      *) echo "foreign ${owner##*|/}" ;;
    esac
  done
}
metrics_containers() { compose ps -a --format '{{.Name}}' 2>/dev/null || true; }
metrics_volumes() { docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT"; }
metric_images() { compose config --images 2>/dev/null || true; }
cluster_images() {  # only if no other k3d cluster on this host needs them
  [ -n "$(docker ps -a --filter label=app=k3d --format '{{.Label "k3d.cluster"}}' | grep -vx "$CLUSTER_NAME")" ] && return
  printf '%s\n' "$K3S_IMAGE"; docker images --format '{{.Repository}}:{{.Tag}}' | grep '^ghcr.io/k3d-io/k3d-' || true
}
present_images() { for i in "$@"; do docker image inspect "$i" >/dev/null 2>&1 && echo "$i"; done; true; }

# ---------- report ----------

LEFT=0
item() {  # item <what should be gone> <evidence it isn't>
  if [ -n "$2" ]; then printf '  ✗ %-36s %s\n' "$1" "$(echo "$2" | tr '\n' ' ')"; LEFT=$((LEFT + 1))
  else printf '  ✓ %s\n' "$1"; fi
}
kept() { [ -n "$2" ] && printf '  · %-36s %s\n' "$1" "$(echo "$2" | tr '\n' ' ')"; true; }

remote_check() {
  local out
  echo "$1:"
  if ! out=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" '
      [ -e /usr/local/bin/k3s ] && echo "agent /usr/local/bin/k3s"
      [ -e /etc/systemd/system/k3s-agent.service ] && echo "agent k3s-agent.service"
      [ -d /var/lib/rancher/k3s ] && echo "agent /var/lib/rancher/k3s"
      for i in flannel.1 cni0; do ip link show "$i" >/dev/null 2>&1 && echo "net $i"; done
      [ -e /etc/udev/rules.d/90-kubesage-flannel.rules ] && echo "udev 90-kubesage-flannel.rules"
      true' 2>/dev/null); then
    echo "  ? can't reach it over SSH (key-based login needed)"; LEFT=$((LEFT + 1)); return
  fi
  item "k3s agent uninstalled" "$(echo "$out" | awk '$1 == "agent" {print $2}')"
  item "flannel/cni interfaces gone" "$(echo "$out" | awk '$1 == "net" {print $2}')"
  item "VXLAN udev rule removed" "$(echo "$out" | awk '$1 == "udev" {print $2}')"
}

check() {
  LEFT=0
  echo "$(hostname) (this machine):"
  item "kubesage serve stopped" "$(ui_pids)"
  item "k3d cluster '$CLUSTER_NAME' deleted" "$(k3d_containers)"
  item "its docker network and volume gone" "$(k3d_leftovers)"
  item "kubeconfig of '$CLUSTER_NAME' removed" "$(own_kubeconfig && echo "$KUBECONFIG" || true)"
  item "ports 6443 8472 10250 8600 free" "$(ss -ltnuH 2>/dev/null | awk '{print $5}' | grep -E ':(6443|8472|10250|8600)$' || true)"
  local mc mv imgs dirs all_images
  mc=$(metrics_containers); mv=$(metrics_volumes)
  mapfile -t all_images < <(metric_images; printf '%s\n' "$K3S_IMAGE"; docker images --format '{{.Repository}}:{{.Tag}}' | grep '^ghcr.io/k3d-io/k3d-' || true)
  imgs=$(present_images "${all_images[@]}"); dirs=$(ls -d "$BIN_DIR" "$REPO_ROOT/out" 2>/dev/null || true)
  if [ "$ALL" = 1 ]; then
    item "VictoriaMetrics + Grafana removed" "$mc"
    item "metrics data volume removed" "$mv"
    item "images removed" "$imgs"
    item ".bin/ and out/ removed" "$dirs"
  else
    kept "kept: metrics stack" "$mc"; kept "kept: metrics history" "$mv"; kept "kept: images" "$imgs"
    kept "kept: tools and runs" "$dirs"
    [ -n "$mc$mv$imgs$dirs" ] && echo "    (--all removes these too)"
  fi
  local n
  for n in "${NODES[@]}"; do remote_check "$n"; done
  [ ${#NODES[@]} -eq 0 ] && [ "$SKIP_NODES" = 0 ] && echo "no joined machines known (.bin/joined-nodes, or pass --node user@host)"
  echo
  if [ "$LEFT" = 0 ]; then echo "Nothing left behind."; else echo "$LEFT thing(s) still there."; fi
}

plan() {
  local pids cs n
  echo "Will remove:"
  pids=$(ui_pids); [ -n "$pids" ] && echo "  - kubesage serve (pid $(echo $pids))"
  for n in "${NODES[@]}"; do echo "  - on $n: drain and delete its node, uninstall the k3s agent, udev rule, kubesage ufw rules"; done
  cs=$(k3d_containers); [ -n "$cs" ] && echo "  - k3d cluster '$CLUSTER_NAME': $(echo $cs), its network and image volume"
  if [ -n "$cs" ]; then
    attached | awk '$1 == "ours" {print "  - detach " $2 " from the cluster network (KubeSage'"'"'s metrics stack)"}'
    attached | awk '$1 == "foreign" {print "  ! " $2 " is on the cluster network but isn'"'"'t KubeSage'"'"'s: left attached, so the network stays"}'
  fi
  own_kubeconfig && echo "  - $KUBECONFIG"
  if [ "$ALL" = 1 ]; then
    [ -n "$(metrics_containers)" ] && echo "  - VictoriaMetrics + Grafana containers and network"
    [ -n "$(metrics_volumes)" ] && echo "  - volume $(metrics_volumes): ALL metrics history (backfill + live)"
    echo "  - images: $(present_images $(metric_images) $(cluster_images) | tr '\n' ' ')"
    echo "  - $BIN_DIR (k3d, kubectl, joined-nodes) and $REPO_ROOT/out (analysis runs)"
  fi
  echo "Keeps: .env, .venv, the repo$( [ "$ALL" = 0 ] && echo ", metrics history, tools, images, runs" )."
}

if [ "$MODE" = check ]; then check; [ "$LEFT" = 0 ]; exit; fi
plan
[ "$MODE" = dry-run ] && exit 0
if [ "$YES" = 0 ]; then
  read -rp "Proceed? [y/N] " answer || answer=""
  [[ "$answer" =~ ^[Yy] ]] || { echo "Nothing changed."; exit 1; }
fi

# ---------- clean ----------

pids=$(ui_pids)
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null || true
  for _ in $(seq 20); do [ -z "$(ui_pids)" ] && break; sleep 0.25; done
  [ -n "$(ui_pids)" ] && kill -9 $(ui_pids) 2>/dev/null || true
  echo "stopped kubesage serve"
fi

if [ ${#NODES[@]} -gt 0 ]; then  # before the cluster goes, so their nodes can still be drained
  if [ -z "${SUDO_PASSWORD:-}" ]; then read -rsp "sudo password on ${NODES[*]}: " SUDO_PASSWORD; echo; fi
  export SUDO_PASSWORD
  for n in "${NODES[@]}"; do
    "$REPO_ROOT/scripts/leave-node.sh" "$n" || echo "warning: $n was not cleaned; retry with scripts/leave-node.sh $n" >&2
  done
fi

if [ -n "$(k3d_containers)" ]; then
  # live-up.sh attaches VictoriaMetrics to the cluster network: detach the compose stack's containers so the
  # network can go. Anything else on it isn't KubeSage's and stays attached; then the network stays too.
  while read -r who name; do
    if [ "$who" = ours ]; then
      docker network disconnect -f "k3d-$CLUSTER_NAME" "$name" && echo "detached $name from k3d-$CLUSTER_NAME"
    else
      echo "warning: $name is attached to k3d-$CLUSTER_NAME and isn't KubeSage's; left it (and the network) alone." >&2
      echo "         When it's safe: docker network disconnect k3d-$CLUSTER_NAME $name && scripts/cleanup.sh" >&2
    fi
  done < <(attached)
  if command -v k3d >/dev/null; then
    # k3d also edits $KUBECONFIG; give it a scratch file so a kubeconfig of another cluster is never rewritten.
    scratch=$(mktemp)
    KUBECONFIG=$scratch k3d cluster delete "$CLUSTER_NAME" || echo "warning: k3d cluster delete did not finish" >&2
    rm -f "$scratch"
  else  # .bin/ is already gone: do what `k3d cluster delete` does
    docker rm -f $(k3d_containers) >/dev/null
  fi
fi
for x in $(k3d_leftovers); do docker network rm "$x" >/dev/null 2>&1 || docker volume rm "$x" >/dev/null 2>&1 || true; done
own_kubeconfig && rm -f "$KUBECONFIG" && echo "removed $KUBECONFIG"

if [ "$ALL" = 1 ]; then
  mapfile -t images < <(present_images $(metric_images) $(cluster_images))
  compose down --volumes --remove-orphans
  for i in "${images[@]}"; do
    if docker rmi "$i" >/dev/null 2>&1; then echo "removed image $i"; else echo "kept image $i: another container uses it"; fi
  done
  rm -rf "$BIN_DIR" "$REPO_ROOT/out"
  echo "removed $BIN_DIR and $REPO_ROOT/out"
fi

echo
check
[ "$LEFT" = 0 ]
