#!/usr/bin/env bash
# Download pinned k3d and kubectl into ./.bin (no sudo, nothing installed system-wide).
set -euo pipefail
source "$(dirname "$0")/versions.sh"
mkdir -p "$BIN_DIR"
cd "$BIN_DIR"

arch=$(uname -m); case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac
os=$(uname -s | tr '[:upper:]' '[:lower:]')

if ! ./k3d version 2>/dev/null | grep -q "$K3D_VERSION"; then
  echo "downloading k3d $K3D_VERSION"
  base="https://github.com/k3d-io/k3d/releases/download/$K3D_VERSION"
  curl -fsSLo "k3d-$os-$arch" "$base/k3d-$os-$arch"
  sum=$(curl -fsSL "$base/checksums.txt" | awk -v f="_dist/k3d-$os-$arch" '$2 == f {print $1}')
  echo "$sum  k3d-$os-$arch" | sha256sum -c -
  mv "k3d-$os-$arch" k3d && chmod +x k3d
fi

if ! ./kubectl version --client 2>/dev/null | grep -q "$KUBECTL_VERSION"; then
  echo "downloading kubectl $KUBECTL_VERSION"
  base="https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch"
  curl -fsSLo kubectl "$base/kubectl"
  echo "$(curl -fsSL "$base/kubectl.sha256")  kubectl" | sha256sum -c -
  chmod +x kubectl
fi

./k3d version | head -1
./kubectl version --client | head -1
