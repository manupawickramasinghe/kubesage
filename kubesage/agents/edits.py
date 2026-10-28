"""Apply and validate the Executor's edits, and the deterministic edits used in mock mode (and as a labelled
fallback when every model-written edit for an action fails validation)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from kubesage.agents.schemas import Action, Edit


class EditError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """safe_load silently keeps the last of two equal keys; a duplicated label or field is a bad edit."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(k, deep=deep) for k, _ in node.value]
        dupes = sorted({str(k) for k in keys if keys.count(k) > 1})
        if dupes:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key(s) {', '.join(dupes)}", node.start_mark)
        return super().construct_mapping(node, deep)


def check_path(repo: Path, path: str) -> Path:
    if not re.fullmatch(r"gitops/[A-Za-z0-9._/-]+", path) or ".." in path.split("/"):
        raise EditError(f"{path}: edits must stay under gitops/")
    return repo / path


def apply_edit(text: str | None, edit: Edit) -> str:
    """The file's new text. `text` is None when the file doesn't exist yet."""
    if edit.find == "":
        if text is not None:
            raise EditError(f"{edit.path}: empty find creates a file, but it already exists")
        return edit.replace
    if text is None:
        raise EditError(f"{edit.path}: no such file")
    count = text.count(edit.find)
    if count == 1:
        return text.replace(edit.find, edit.replace, 1)
    if count == 0 and (fixed := _reindented(text, edit)) is not None:
        return fixed
    raise EditError(f"{edit.path}: find text occurs {count} times, must occur exactly once")


def _reindented(text: str, edit: Edit) -> str | None:
    """Models often get indentation wrong. Accept a find whose lines match one unique block of the file once leading
    and trailing whitespace is ignored, and shift the replacement by the same indentation difference."""
    want = [line.strip() for line in edit.find.strip("\n").splitlines()]
    if not want or not any(want):
        return None
    lines = text.splitlines(keepends=True)
    hits = [i for i in range(len(lines) - len(want) + 1)
            if [line.strip() for line in lines[i:i + len(want)]] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    first_find = edit.find.strip("\n").splitlines()[0]
    shift = (len(lines[i]) - len(lines[i].lstrip())) - (len(first_find) - len(first_find.lstrip()))
    new = []
    for line in edit.replace.strip("\n").splitlines():
        new.append(" " * shift + line if shift >= 0 else line[min(-shift, len(line) - len(line.lstrip())):])
    end = "\n" if lines[i + len(want) - 1].endswith("\n") else ""
    return "".join(lines[:i]) + "\n".join(new) + end + "".join(lines[i + len(want):])


def validate_file(path: str, text: str, skus: set[str]) -> None:
    if path.endswith((".yaml", ".yml")):
        try:
            docs = [d for d in yaml.load_all(text, Loader=_UniqueKeyLoader) if d is not None]
        except yaml.YAMLError as exc:
            raise EditError(f"{path}: YAML does not parse: {str(exc).splitlines()[0]}") from exc
        for doc in docs:
            if not isinstance(doc, dict) or ("kind" not in doc and "resources" not in doc):
                raise EditError(f"{path}: every YAML document must be a Kubernetes object or a Kustomization")
    elif path.endswith(".py"):
        try:
            compile(text, path, "exec")
        except SyntaxError as exc:
            raise EditError(f"{path}: Python does not compile: line {exc.lineno}: {exc.msg}") from exc
    elif path.endswith(".tf"):
        if text.count("{") != text.count("}"):
            raise EditError(f"{path}: unbalanced braces")
        for size in re.findall(r'vm_size\s*=\s*"([^"]+)"', text):
            if not size.startswith("Standard_") or size.removeprefix("Standard_") not in skus:
                raise EditError(f"{path}: vm_size {size} is not in config/skus.yaml")


# ---- deterministic edits ----

def _flow(d: dict) -> str:
    return "{" + ", ".join(f"{k}: {_q(v)}" for k, v in d.items()) + "}"


def _q(v) -> str:
    s = str(v)
    return f'"{s}"' if re.fullmatch(r"[0-9.]+", s) else s


def _line_edit(path: str, text: str, pattern: str, new: str, action: str) -> Edit | None:
    m = re.search(pattern, text, re.M)
    return Edit(path=path, find=m.group(0), replace=new(m) if callable(new) else new, action=action) if m else None


def resources_edits(path: str, text: str, action: Action) -> list[Edit]:
    edits = []
    for key in ("requests", "limits"):
        values = (action.params or {}).get(key)
        if not values:
            continue
        m = re.search(rf"^(\s*){key}: \{{([^}}]*)\}}", text, re.M)
        if not m:
            continue
        current = dict(part.split(": ", 1) for part in m.group(2).split(", ") if ": " in part)
        current.update({k: str(v) for k, v in values.items() if v is not None})
        edits.append(Edit(path=path, find=m.group(0), replace=f"{m.group(1)}{key}: {_flow(current)}", action=action.id))
    return edits


def deterministic(action: Action, files: dict[str, str]) -> list[Edit]:
    """Edits for the action types that need no judgement. [] when the action needs a model (or a person)."""
    p, target = action.params or {}, action.target
    name = target.rsplit("/", 1)[-1]
    dep = f"gitops/apps/{name}/deployment.yaml"
    tf = "gitops/infra/nodepools.tf"
    if action.type == "set_resources" and dep in files:
        return resources_edits(dep, files[dep], action)
    if action.type == "change_node_sku" and tf in files and p.get("to_sku"):
        pool = p.get("pool") or name
        block = _pool_block(files[tf], pool)
        if not block:
            return []
        new = re.sub(r'vm_size(\s*)= "Standard_[^"]+"', lambda m: f'vm_size{m.group(1)}= "Standard_{p["to_sku"]}"', block)
        new = re.sub(r'("kubesage.io/sku"\s*= )"[^"]+"', lambda m: f'{m.group(1)}"{p["to_sku"]}"', new)
        return [Edit(path=tf, find=block, replace=new, action=action.id)] if new != block else []
    if action.type == "set_node_pool" and tf in files:
        pool = p.get("pool") or name
        block = _pool_block(files[tf], pool)
        if not block:
            return []
        new = block
        for key in ("node_count", "max_pods"):
            if p.get(key):
                new = re.sub(rf"({key}\s*= )\d+", lambda m: f"{m.group(1)}{int(p[key])}", new)
        return [Edit(path=tf, find=block, replace=new, action=action.id)] if new != block else []
    size = p.get("size_limit") or p.get("sizeLimit") or p.get("size")
    if action.type == "storage" and dep in files and size:
        vol = p.get("volume")
        found = re.findall(r"- \{name: [\w-]+, emptyDir: \{sizeLimit: [^}]+\}\}", files[dep])
        named = [f for f in found if vol and f.startswith(f"- {{name: {vol},")]
        target_line = named[0] if named else found[0] if len(found) == 1 else None  # one emptyDir: no doubt which
        if target_line:
            return [Edit(path=dep, find=target_line, replace=re.sub(r"sizeLimit: [^}]+", f"sizeLimit: {size}", target_line),
                         action=action.id)]
    if action.type == "autoscale" and dep in files:
        kust = f"gitops/apps/{name}/kustomization.yaml"
        so = scaled_object(name, p)
        out = [Edit(path=f"gitops/apps/{name}/scaledobject.yaml", find="", replace=so, action=action.id)]
        m = re.search(r"^resources: \[([^\]]*)\]", files.get(kust, ""), re.M)
        if m and "scaledobject.yaml" not in m.group(1):
            out.append(Edit(path=kust, find=m.group(0), replace=f"resources: [{m.group(1)}, scaledobject.yaml]", action=action.id))
        return out
    if action.type == "spread_pods" and dep in files and p.get("replicas"):
        e = _line_edit(dep, files[dep], r"^  replicas: \d+$", f"  replicas: {int(p['replicas'])}", action.id)
        return [e] if e else []
    if action.type == "security_hardening" and target.startswith("namespace/"):
        path = "gitops/apps/namespace.yaml"
        level = p.get("pod_security", "baseline")
        m = re.search(r"^  labels:\n(    .+\n)+", files.get(path, ""), re.M)
        if not re.search(rf"^  name: {re.escape(name)}$", files.get(path, ""), re.M):
            return []  # only the lab namespace is in gitops/
        if m and "pod-security.kubernetes.io/enforce" not in m.group(0):
            add = (f"    pod-security.kubernetes.io/enforce: {level}\n    pod-security.kubernetes.io/warn: restricted\n"
                   f"    pod-security.kubernetes.io/audit: restricted\n")
            return [Edit(path=path, find=m.group(0), replace=m.group(0) + add, action=action.id)]
    if action.type == "code_change":
        src = p.get("file") or next((f for f in files if f.startswith(f"gitops/apps/{name}/src/")), None)
        text = files.get(src or "", "")
        if "ThreadPoolExecutor" in text and "def " in text:
            # CPU-bound pure-Python work under the GIL: the same pool API, on processes.
            return [Edit(path=src, find=text, replace=text.replace("ThreadPoolExecutor", "ProcessPoolExecutor"),
                         action=action.id)]
    return []


def _pool_block(tf: str, pool: str) -> str | None:
    m = re.search(rf'resource "azurerm_kubernetes_cluster_node_pool" "{re.escape(pool)}" \{{.*?\n\}}', tf, re.S)
    if m:
        return m.group(0)
    m = re.search(r"  default_node_pool \{.*?\n  \}", tf, re.S)
    return m.group(0) if m and f'name       = "{pool}"' in m.group(0) else None


def scaled_object(name: str, p: dict) -> str:
    threshold = p.get("threshold", 60)
    return f"""# KEDA autoscaling for {name}, added by the KubeSage Executor.
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: {name}
  labels: {{app: {name}, app.kubernetes.io/managed-by: kubesage}}
spec:
  scaleTargetRef: {{name: {name}}}
  minReplicaCount: {int(p.get('min_replicas', 1))}
  maxReplicaCount: {int(p.get('max_replicas', 4))}
  triggers:
    - type: cpu
      metricType: Utilization
      metadata: {{value: "{int(threshold)}"}}
"""
