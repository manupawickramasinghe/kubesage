#!/bin/sh
# In-cluster CronJob entrypoint (deploy/k8s/kubesage/cronjob.yaml): analyze, then commit
# the result to the GitOps repo as a branch. Flux never sees this run until a human
# merges the branch/PR — merging is the apply. KubeSage itself writes nothing to the
# cluster API (its ServiceAccount is read-only).
set -eu

: "${KUBESAGE_GITOPS_REPO_URL:?set KUBESAGE_GITOPS_REPO_URL (ssh URL of the GitOps repo)}"
: "${KUBESAGE_VM_URL:?set KUBESAGE_VM_URL (Prometheus/VictoriaMetrics reachable from the pod)}"
TARGET_NAMESPACE="${KUBESAGE_TARGET_NAMESPACE:-demo}"
GITOPS_PATH="${KUBESAGE_GITOPS_PATH:-kubesage/${TARGET_NAMESPACE}}"
KUSTOMIZE_BASE="${KUBESAGE_KUSTOMIZE_BASE:-../base}"

export GIT_SSH_COMMAND="ssh -i /secrets/git/ssh-privatekey -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git config --global user.name "${KUBESAGE_GIT_USER_NAME:-KubeSage}"
git config --global user.email "${KUBESAGE_GIT_USER_EMAIL:-kubesage@localhost}"
git config --global --add safe.directory /tmp/gitops-repo

git clone --quiet "$KUBESAGE_GITOPS_REPO_URL" /tmp/gitops-repo
kubesage analyze --mode live --vm "$KUBESAGE_VM_URL" --namespace "$TARGET_NAMESPACE" --out-dir /tmp/out
kubesage gitops --repo /tmp/gitops-repo --kustomize-base "$KUSTOMIZE_BASE" --path "$GITOPS_PATH" \
  --out-dir /tmp/out --push
echo "Branch pushed. Open/merge the PR to let Flux apply it."
