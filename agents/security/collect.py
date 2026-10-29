"""Security sub-agent: pod posture, GitOps drift, RBAC and probes against the Control Center. Read-only."""
import re
import time
from pathlib import Path

import yaml

_REPLICA = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[a-z0-9]{5}$")
POWERFUL = {"cluster-admin", "admin", "edit"}


def gitops_names(gitops: Path):
    names = set()
    for path in gitops.glob("apps/**/*.yaml"):
        try:
            for doc in yaml.safe_load_all(path.read_text()):
                if isinstance(doc, dict) and doc.get("kind") not in (None, "Kustomization"):
                    names.add((doc["kind"], doc.get("metadata", {}).get("name")))
        except (OSError, yaml.YAMLError):
            continue
    return names


def image_repo(image: str) -> str:
    """registry/path of an image reference, the way the runtime resolves it: no tag or digest, Docker Hub spelled
    out (python -> docker.io/library/python)."""
    name = image.split("@", 1)[0]
    if ":" in name.split("/")[-1]:
        name = name.rsplit(":", 1)[0]
    first, _, rest = name.partition("/")
    if not rest or ("." not in first and ":" not in first and first != "localhost"):
        first, rest = "docker.io", name if "/" in name else f"library/{name}"
    return f"{'docker.io' if first == 'index.docker.io' else first}/{rest}"


def allowed_image(image: str, allowed: list[str]) -> bool:
    """A whole registry (registry.k8s.io) or a repository and what's under it, matched on "/" boundaries, so
    python-malicious isn't python and registry.k8s.io.attacker isn't registry.k8s.io."""
    repo = image_repo(image)
    for a in allowed:
        if "/" not in a and ("." in a or ":" in a or a == "localhost"):  # a bare registry
            if repo.split("/", 1)[0] == a:
                return True
        elif repo == image_repo(a) or repo.startswith(image_repo(a) + "/"):
            return True
    return False


def collect(ctx):
    allowed = ctx.settings.get("allowed_registries", [])
    in_git = gitops_names(ctx.gitops_dir)
    for namespace in ctx.namespaces:
        ns = ctx.kube("get", "namespace", namespace)
        if ns:
            labels = ns["metadata"].get("labels", {})
            if "pod-security.kubernetes.io/enforce" not in labels:
                ctx.signal(f"{namespace}.psa", "namespace_no_pod_security", f"namespace/{namespace}", "unset",
                           detail="no pod-security.kubernetes.io/enforce label: any pod spec is admitted",
                           severity="info")
        seen = set()
        for pod in (ctx.kube("-n", namespace, "get", "pods") or {}).get("items", []):
            meta, spec = pod["metadata"], pod["spec"]
            owners = meta.get("ownerReferences") or []
            name = _REPLICA.sub("", meta["name"]) if owners else meta["name"]
            if name in seen:
                continue
            seen.add(name)
            target = f"{'deployment' if owners else 'pod'}/{namespace}/{name}"
            problems = []
            psc = spec.get("securityContext") or {}
            for c in spec.get("containers", []):
                sc = c.get("securityContext") or {}
                if not (sc.get("runAsNonRoot") or psc.get("runAsNonRoot")) and not (sc.get("runAsUser") or psc.get("runAsUser")):
                    problems.append(f"{c['name']}: no runAsNonRoot/runAsUser, runs as the image's user (root unless the image sets one)")
                if sc.get("privileged"):
                    problems.append(f"{c['name']}: privileged")
                if (sc.get("capabilities") or {}).get("add"):
                    problems.append(f"{c['name']}: adds capabilities {sc['capabilities']['add']}")
                image = c.get("image", "")
                repo, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "")
                if not tag or tag == "latest":
                    problems.append(f"{c['name']}: floating image tag ({image})")
                if allowed and not allowed_image(image, allowed):
                    problems.append(f"{c['name']}: image {image} is from outside the allowed registries")
                if not (c.get("resources") or {}).get("limits"):
                    problems.append(f"{c['name']}: no resource limits")
            if spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC"):
                problems.append("shares a host namespace")
            if any("hostPath" in v for v in spec.get("volumes", [])):
                problems.append("mounts a hostPath volume")
            if problems:
                ctx.signal(f"{name}.posture", "pod_posture", target, len(problems), unit="problems",
                           detail="; ".join(problems), severity="warning" if len(problems) >= 2 else "info")
            kind = "Deployment" if owners else "Pod"
            if namespace == "lab" and (kind, name) not in in_git:
                managers = sorted({f.get("manager", "?") for f in meta.get("managedFields", [])})
                ctx.signal(f"{name}.drift", "not_in_gitops", target, kind,
                           detail=f"running since {meta.get('creationTimestamp')} but not in gitops/; "
                                  f"written by {', '.join(managers)}; {'bare pod, no controller' if not owners else ''}",
                           severity="warning")
    for kind, args in (("RoleBinding", lambda ns: ("-n", ns, "get", "rolebindings")),):
        for namespace in ctx.namespaces:
            for b in (ctx.kube(*args(namespace)) or {}).get("items", []):
                if b["roleRef"]["name"] in POWERFUL:
                    subjects = [f"{s['kind']}/{s.get('namespace', '')}/{s['name']}" for s in b.get("subjects", [])]
                    ctx.signal(f"{namespace}.{b['metadata']['name']}.rbac", "powerful_binding",
                               f"namespace/{namespace}", b["roleRef"]["name"], detail=f"{kind} {b['metadata']['name']} -> {subjects}",
                               severity="warning")
    for b in (ctx.kube("get", "clusterrolebindings") or {}).get("items", []):
        if b["roleRef"]["name"] != "cluster-admin" or b["metadata"]["name"].startswith("system:"):
            continue
        subjects = [s for s in b.get("subjects", []) if not s["name"].startswith("system:")]
        if subjects:
            ctx.signal(f"crb.{b['metadata']['name']}", "cluster_admin_binding", "cluster", b["metadata"]["name"],
                       detail=f"cluster-admin -> {[s['kind'] + '/' + s['name'] for s in subjects]}", severity="info")

    known = ctx.settings.get("known_paths", ["/"])
    cutoff = time.time() - 900
    by_client = {}
    for r in ctx.access_log():
        if r["at"] < cutoff:
            continue
        c = by_client.setdefault(r["client"], {"requests": 0, "unknown": set(), "csrf": 0, "refused": [], "agents": set()})
        c["requests"] += 1
        c["agents"].add(r.get("agent", "")[:60])
        path = r["path"].split("?", 1)[0]
        if r["status"] == 404 and not any(path == k or (k != "/" and k.endswith("/") and path.startswith(k)) for k in known):
            c["unknown"].add(path[:80])
        if r["status"] == 403 and r["method"] == "POST":
            c["csrf"] += 1
        if r.get("refused"):
            c["refused"].append(r["refused"][:80])
    for client, c in sorted(by_client.items()):
        if c["unknown"]:
            ctx.signal(f"web.{client}.probe", "web_unknown_paths", f"client/{client}", len(c["unknown"]), unit="paths",
                       detail=f"404s for {sorted(c['unknown'])[:12]} out of {c['requests']} requests; user agents {sorted(c['agents'])[:3]}",
                       severity="warning" if len(c["unknown"]) >= 3 else "info")
        if c["csrf"]:
            ctx.signal(f"web.{client}.csrf", "web_csrf_rejected", f"client/{client}", c["csrf"], unit="POSTs",
                       detail="POSTs without the X-KubeSage header (what a cross-site request or a raw script sends)",
                       severity="warning" if c["csrf"] >= 3 else "info")
        if c["refused"]:
            ctx.signal(f"web.{client}.shell", "web_shell_refused", f"client/{client}", len(c["refused"]), unit="commands",
                       detail=f"refused by the read-only shell: {c['refused'][:8]}",
                       severity="warning" if len(c["refused"]) >= 3 else "info")
    ctx.log(f"{len(ctx.signals)} signals; {len(by_client)} web clients in the last 15 min")
