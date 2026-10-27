"""`kubesage spike`: a load-generator Job against a demo Service, for the live part of the demo."""
from __future__ import annotations

import subprocess
import time

import yaml

from kubesage.apply import kubectl_base

APP_IMAGE = "python:3.13.12-alpine3.22"  # keep in sync with deploy/k8s/demo/workloads.yaml


def spike_job(*, namespace: str, target: str, rps: float, work_ms: int, seconds: int) -> dict:
    name = f"kubesage-spike-{int(time.time())}"
    env = {"TARGET": f"http://{target}:8080", "RPS": str(rps), "WORK_MS": str(work_ms), "SECONDS": str(seconds)}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": "kubesage"}},
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": {"app": "loadgen"}}, "spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": "loadgen", "image": APP_IMAGE, "command": ["python", "/app/app.py", "loadgen"],
                    "env": [{"name": k, "value": v} for k, v in env.items()],
                    "volumeMounts": [{"name": "app", "mountPath": "/app"}],
                    "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                }],
                "volumes": [{"name": "app", "configMap": {"name": "demo-app"}}],
            }},
        },
    }


def start_spike(job: dict) -> subprocess.CompletedProcess:
    return subprocess.run([*kubectl_base(), "apply", "-f", "-"], input=yaml.safe_dump(job), capture_output=True, text=True,
                          timeout=30)
