# Pinned tool and image versions, sourced by the other scripts.
# KEDA 2.21 supports Kubernetes 1.34-1.36; keep K3S_IMAGE and KUBECTL_VERSION on the same minor.
K3D_VERSION=v5.9.0
KUBECTL_VERSION=v1.36.4
K3S_IMAGE=rancher/k3s:v1.36.4-k3s1
KEDA_VERSION=2.21.0
CLUSTER_NAME=kubesage

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/.bin"
export PATH="$BIN_DIR:$PATH"
PY="$REPO_ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
