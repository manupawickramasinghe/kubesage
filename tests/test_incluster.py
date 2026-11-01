"""In-cluster deployment: image, read-only RBAC, CronJob+UI manifests, Flux examples."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy/k8s/kubesage"


def _docs(*names):
    out = []
    for name in names:
        out += [d for d in yaml.safe_load_all((DEPLOY / name).read_text()) if d]
    return out


def test_dockerfile_bakes_no_secret_and_installs_the_cli():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "pip install" in dockerfile and "kubesage" in dockerfile
    code = "\n".join(l for l in dockerfile.splitlines() if not l.lstrip().startswith("#"))
    assert "MODEL_API_KEY=" not in code and "LLM|" not in code
    ignore = (ROOT / ".dockerignore").read_text()
    assert ".env" in ignore and ".venv" in ignore


def test_rbac_is_read_only():
    rbac = _docs("rbac.yaml")
    role = next(d for d in rbac if d["kind"] == "ClusterRole")
    verbs = {v for rule in role["rules"] for v in rule["verbs"]}
    assert verbs <= {"get", "list", "watch"}, f"write verbs leaked into ClusterRole: {verbs}"
    assert all(d["metadata"].get("namespace", "kubesage") == "kubesage" for d in rbac if d["kind"] != "ClusterRoleBinding" or True)


def test_kustomization_resources_exist_and_examples_are_excluded():
    kust = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text())
    for name in kust["resources"]:
        assert (DEPLOY / name).exists(), name
    assert not any("example" in r for r in kust["resources"])


def test_ui_and_cronjob_share_image_env_and_pinned_requests():
    deployment, cronjob = _docs("deployment.yaml"), _docs("cronjob.yaml")
    dep = next(d for d in deployment if d["kind"] == "Deployment")
    job = next(d for d in cronjob if d["kind"] == "CronJob")
    pod = dep["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "kubesage"
    container = pod["containers"][0]
    assert container["args"][:1] == ["serve"]
    assert container["resources"]["requests"] and container["resources"]["limits"]
    spec = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    analyze = spec["containers"][0]
    assert analyze["command"] == ["sh", "/app/scripts/in-cluster-analyze.sh"]
    assert (ROOT / "scripts/in-cluster-analyze.sh").exists()
    env = {e["name"] for e in analyze["env"]}
    assert {"MODEL_API_KEY", "KUBESAGE_VM_URL", "KUBESAGE_GITOPS_REPO_URL"} <= env
    assert job["spec"]["concurrencyPolicy"] == "Forbid"


def test_secrets_are_placeholders_and_flux_examples_are_valid():
    for name in ("secret.example.yaml", "configmap.example.yaml"):
        text = (DEPLOY / name).read_text()
        assert "REPLACE-ME" in text or name.startswith("configmap")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert "LLM|" not in body
    for name in ("gitrepository.yaml", "kustomization.yaml"):
        docs = [d for d in yaml.safe_load_all((DEPLOY / "flux" / name).read_text()) if d]
        assert docs and all(d["apiVersion"].startswith(("source.toolkit", "kustomize.toolkit")) for d in docs)
