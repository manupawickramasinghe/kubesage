"""GitOps export into a throwaway repo with a local bare remote; no network, no gh."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from kubesage import gitops, summary
from kubesage.pipeline import write_run

ROOT = Path(__file__).resolve().parent.parent
KUBECTL = ROOT / ".bin/kubectl"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def gitops_repo(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "gitops"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    shutil.copytree(ROOT / "deploy/k8s/demo", repo / "base")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


def test_export_commits_overlay_on_branch_and_pushes(result, tmp_path, gitops_repo):
    repo, remote = gitops_repo
    run = write_run(result, tmp_path / "runs/20260101-000000")
    out = gitops.export(run, repo, path="kubesage/demo", kustomize_base="../../base", push=True)

    assert out.branch == "kubesage/rightsize-20260101-000000"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # left where it was
    assert git(remote, "rev-parse", out.branch) == out.commit
    assert git(repo, "log", "-1", "--format=%s", out.branch) == summary.title(result)
    files = git(repo, "ls-tree", "--name-only", out.branch, "kubesage/demo/").split("\n")
    assert "kubesage/demo/kustomization.yaml" in files and "kubesage/demo/REPORT.md" in files
    assert "kubesage/demo/checkout-web.keda.yaml" in files

    with pytest.raises(gitops.GitOpsError):  # same branch twice must not clobber anything
        gitops.export(run, repo, path="kubesage/demo", kustomize_base="../../base")


@pytest.mark.skipif(not KUBECTL.exists(), reason="needs .bin/kubectl (scripts/install-tools.sh)")
def test_overlay_builds_with_kustomize(result, tmp_path, gitops_repo):
    repo, _ = gitops_repo
    run = write_run(result, tmp_path / "runs/20260101-000000")
    out = gitops.export(run, repo, path="kubesage/demo", kustomize_base="../../base")
    git(repo, "switch", "-q", out.branch)
    built = subprocess.run([str(KUBECTL), "kustomize", str(repo / "kubesage/demo")], check=True,
                           capture_output=True, text=True).stdout
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(built)}
    payment = docs[("Deployment", "payment-service")]["spec"]
    rec = next(w.recommendation for w in result.workloads if w.id == "demo/payment-service")
    assert payment["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] == f"{rec.resources.cpu_request_m}m"
    assert payment["template"]["spec"]["nodeSelector"] == {"kubesage.io/sku": result.node_pool.choice.sku}
    assert "replicas" not in docs[("Deployment", "checkout-web")]["spec"]  # the ScaledObject owns it now
    assert docs[("Deployment", "catalog-api")]["spec"]["replicas"] == 2  # healthy and unscaled: untouched
    assert ("ScaledObject", "report-worker") in docs


def test_report_keeps_the_node_pool_notes_and_burst_warning(result):
    # Review F17: REPORT.md (the GitOps PR body) dropped the pool notes, including the burst-capacity warning.
    assert result.node_pool and result.node_pool.notes
    md = summary.markdown(result)
    for note in result.node_pool.notes:
        assert f"- note: {note}" in md
