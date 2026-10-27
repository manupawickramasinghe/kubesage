#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/versions.sh"
k3d cluster delete "$CLUSTER_NAME"
rm -f "$BIN_DIR/kubeconfig"
