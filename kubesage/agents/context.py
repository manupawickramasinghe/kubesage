"""What a sub-agent's collect(ctx) can read: VictoriaMetrics, the Kubernetes API, Alertmanager, the gitops/
folder and the Control Center's own request log. Everything is read-only, and every call is recorded for the
run's trace."""
from __future__ import annotations

import json
import math
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from kubesage import ring
from kubesage.apply import kubectl_base
from kubesage.metrics.client import PromClient, PromError


class Context:
    def __init__(self, *, agent: str, settings: dict, vm: str, alertmanager: str, namespaces: list[str],
                 gitops_dir: Path, access_log: Callable[[], list[dict]] | None = None, catalog=None):
        self.agent, self.settings = agent, settings
        self.vm, self.alertmanager = vm, alertmanager.rstrip("/")
        self.namespaces = list(settings.get("namespaces") or namespaces)
        self.gitops_dir = gitops_dir
        self.catalog = catalog
        self._access_log = access_log or (lambda: [])
        self.prom = PromClient(vm, timeout=15, journal=False)
        self.calls: list[dict] = []  # kubectl and HTTP calls; PromQL calls are in self.prom.calls
        self.signals: list[dict] = []

    # ---- output ----
    def log(self, text: str) -> None:
        ring.emit(f"agent/{self.agent}", text)

    def signal(self, id: str, kind: str, target: str, value, *, unit: str = "", detail: str = "",
               severity: str | None = None, source: str = "") -> dict:
        """Record one fact. `severity` is the collector's own threshold verdict (None: normal), a hint for the model."""
        if isinstance(value, float):
            value = round(value, 4) if math.isfinite(value) else None
        sig = {"id": f"{self.agent}:{id}", "kind": kind, "target": target, "value": value, "unit": unit,
               "detail": detail, "severity": severity, "source": source}
        self.signals.append(sig)
        return sig

    # ---- reads ----
    def query(self, expr: str) -> list[tuple[dict, float]]:
        """An instant PromQL query, as (labels, value) pairs. Empty if VictoriaMetrics is unreachable."""
        try:
            return [(r["metric"], float(r["value"][1])) for r in self.prom.query(expr)]
        except (PromError, KeyError, ValueError, IndexError):
            return []

    def kube(self, *args: str) -> dict | None:
        args = (*args, "-o", "json")
        return self._kubectl(list(args), "kubectl " + " ".join(args))

    def kube_raw(self, path: str) -> dict | None:
        return self._kubectl(["get", "--raw", path], f"kubectl get --raw {path}")

    def _kubectl(self, args: list[str], shown: str) -> dict | None:
        started = time.perf_counter()
        call = {"kind": "kubectl", "request": {"cmd": shown}}
        self.calls.append(call)
        try:
            proc = subprocess.run([*kubectl_base(), "--request-timeout=8s", *args], capture_output=True, text=True,
                                  timeout=12)
            data = json.loads(proc.stdout) if proc.returncode == 0 else None
            call["response"] = {"exit": proc.returncode, "items": len(data.get("items", [])) if isinstance(data, dict) else None,
                                "error": proc.stderr.strip()[:300] if proc.returncode else None}
            return data
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            call["response"] = {"error": str(exc)}
            return None
        finally:
            call["latency_ms"] = int((time.perf_counter() - started) * 1000)

    def http_json(self, url: str):
        started = time.perf_counter()
        call = {"kind": "http", "request": {"method": "GET", "url": url}}
        self.calls.append(call)
        try:
            resp = httpx.get(url, timeout=8)
            call["response"] = {"status": resp.status_code}
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            call["response"] = {"error": str(exc)}
            return None
        finally:
            call["latency_ms"] = int((time.perf_counter() - started) * 1000)

    def access_log(self) -> list[dict]:
        """Requests the Control Center served recently: client, method, path, status, user agent."""
        return self._access_log()

    def trace(self) -> list[dict]:
        return [{**c, "agent": self.agent} for c in self.prom.calls + self.calls]
