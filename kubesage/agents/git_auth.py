"""Git identity for the Executor's clone: an optional dedicated deploy key, kept OUTSIDE the repo.

By default the Executor pushes with the operator's own SSH agent and opens PRs as whoever `gh` is
logged in as. That mixes the agent's writes with the operator's identity and only works while the
operator is logged in. The alternative is a GitHub *deploy key* (repo Settings -> Deploy keys, with
write access): one key, one repo, nothing else — exactly the Executor's blast radius.

Set KUBESAGE_EXECUTOR_SSH_KEY to the private key's path (e.g. ~/.ssh/kubesage-executor, stored in
the service environment / .env, never in the repo). This module validates that path before git ever
sees it and refuses the configurations that end in a leaked key:

- a path inside this repository (private keys in a repo get committed eventually — always);
- a missing file, or a `.pub` public key passed by mistake;
- permissions any group/other user can read (ssh would refuse it anyway; we say why first).

Note the deploy key covers git clone/fetch/push. `gh pr create` still needs GitHub CLI auth
(`gh auth login`, or GH_TOKEN in the service environment) — a deploy key cannot call the API.
"""
from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path

ENV_VAR = "KUBESAGE_EXECUTOR_SSH_KEY"
REPO_ROOT = Path(__file__).resolve().parents[2]


class GitAuthError(RuntimeError):
    pass


def executor_ssh_env(env: dict[str, str] | None = None, repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Environment additions for git subprocesses. Empty when no dedicated key is configured
    (the operator's SSH agent is used, as before). Raises GitAuthError on an unsafe key path."""
    raw = (env if env is not None else os.environ).get(ENV_VAR, "").strip()
    if not raw:
        return {}
    key = Path(raw).expanduser()
    if key.suffix == ".pub":
        raise GitAuthError(f"{ENV_VAR} points at a public key ({key}); git needs the private key")
    if not key.is_file():
        raise GitAuthError(f"{ENV_VAR}={key} does not exist; generate a deploy key outside the repo, e.g. "
                           "ssh-keygen -t ed25519 -f ~/.ssh/kubesage-executor -N ''")
    resolved = key.resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise GitAuthError(f"{ENV_VAR}={key} is inside the repository. A private key must never live in "
                           "the repo (it would be committed and published); move it to ~/.ssh/ instead")
    mode = stat.S_IMODE(key.stat().st_mode)
    if mode & 0o077:
        raise GitAuthError(f"{ENV_VAR}={key} is readable by other users (mode {mode:03o}); "
                           "run: chmod 600 " + str(key))
    command = f"ssh -i {shlex.quote(str(resolved))} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    return {"GIT_SSH_COMMAND": command}
