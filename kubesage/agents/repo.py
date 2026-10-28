"""The Executor's own clone of the repo, under out/agents/repo. Never the checkout the server runs from, which
people and other agents work in: the Executor switches branches and resets freely here."""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class RepoError(RuntimeError):
    pass


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RepoError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout.strip()


def origin_url() -> str:
    return git(REPO_ROOT, "remote", "get-url", "origin")


def github_slug(url: str) -> str | None:
    """owner/name from git@github.com:owner/name.git or https://github.com/owner/name(.git)."""
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix):].removesuffix(".git")
    return None


def ensure(clone: Path, url: str) -> Path:
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", url, str(clone)], check=True,
                       capture_output=True, text=True, timeout=300)
    git(clone, "fetch", "--quiet", "--prune", "origin")
    return clone


def start_branch(clone: Path, base: str, branch: str) -> str:
    """A fresh branch from origin/<base>; returns the base commit."""
    try:
        base_sha = git(clone, "rev-parse", f"origin/{base}")
    except RepoError as exc:
        raise RepoError(f"origin has no branch {base!r}") from exc
    if not git(clone, "ls-tree", "--name-only", f"origin/{base}", "gitops/apps/"):
        raise RepoError(f"origin/{base} has no gitops/apps/ yet: merge the agent-team PR first, "
                        f"or set KUBESAGE_AGENTS_BASE to a branch that has it")
    git(clone, "reset", "--quiet", "--hard")
    git(clone, "clean", "-fdq")
    git(clone, "switch", "--quiet", "--force-create", branch, base_sha)
    return base_sha


def commit(clone: Path, message: str) -> str:
    git(clone, "add", "-A", "gitops")
    git(clone, "commit", "--quiet", "-m", message)
    return git(clone, "rev-parse", "HEAD")


def diff(clone: Path, base_sha: str) -> str:
    return git(clone, "diff", base_sha, "HEAD", "--", "gitops")


def open_pr(clone: Path, *, branch: str, base: str, title: str, body_file: Path) -> str:
    git(clone, "push", "--quiet", "--force-with-lease", "-u", "origin", branch, timeout=180)
    slug = github_slug(git(clone, "remote", "get-url", "origin"))
    if not slug:
        raise RepoError("origin is not a GitHub repo, so there is nowhere to open a pull request")
    proc = subprocess.run(["gh", "pr", "create", "--repo", slug, "--base", base, "--head", branch, "--title", title,
                           "--body-file", str(body_file)], capture_output=True, text=True, timeout=120)
    if proc.returncode:
        existing = subprocess.run(["gh", "pr", "view", branch, "--repo", slug, "--json", "url", "-q", ".url"],
                                  capture_output=True, text=True, timeout=60)
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip()
        raise RepoError(f"gh pr create: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout.strip().splitlines()[-1]
