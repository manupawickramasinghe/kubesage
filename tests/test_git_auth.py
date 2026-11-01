"""Executor deploy-key auth: dedicated identity, validated, never inside the repo."""
import os
from pathlib import Path

import pytest

from kubesage.agents.git_auth import ENV_VAR, GitAuthError, executor_ssh_env


def _key(tmp_path: Path, mode: int = 0o600) -> Path:
    k = tmp_path / "kubesage-executor"
    k.write_text("not-a-real-key")
    os.chmod(k, mode)
    return k


def test_unset_env_keeps_operator_identity():
    assert executor_ssh_env({}) == {}
    assert executor_ssh_env({ENV_VAR: "  "}) == {}


def test_valid_external_key_builds_ssh_command(tmp_path):
    k = _key(tmp_path)
    env = executor_ssh_env({ENV_VAR: str(k)}, repo_root=tmp_path / "repo")
    assert env["GIT_SSH_COMMAND"].startswith(f"ssh -i {k.resolve()}")
    assert "IdentitiesOnly=yes" in env["GIT_SSH_COMMAND"]


def test_key_inside_the_repo_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    k = _key(repo)
    with pytest.raises(GitAuthError, match="inside the repository"):
        executor_ssh_env({ENV_VAR: str(k)}, repo_root=repo)


def test_missing_and_public_keys_are_refused(tmp_path):
    with pytest.raises(GitAuthError, match="does not exist"):
        executor_ssh_env({ENV_VAR: str(tmp_path / "nope")}, repo_root=tmp_path / "repo")
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA")
    with pytest.raises(GitAuthError, match="public key"):
        executor_ssh_env({ENV_VAR: str(pub)}, repo_root=tmp_path / "repo")


def test_world_readable_key_is_refused(tmp_path):
    k = _key(tmp_path, mode=0o644)
    with pytest.raises(GitAuthError, match="chmod 600"):
        executor_ssh_env({ENV_VAR: str(k)}, repo_root=tmp_path / "repo")
