#!/usr/bin/env bash
# Delete the cluster, including the k3s agent on joined machines, without asking. Keeps the metrics history,
# tools and runs. Shorthand for `scripts/cleanup.sh --yes`; see that script, or docs/cleanup.md, for --all etc.
exec "$(dirname "$0")/cleanup.sh" --yes "$@"
