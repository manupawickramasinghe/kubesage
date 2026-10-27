"""Minimal Prometheus HTTP API client. Works against Prometheus and VictoriaMetrics alike."""
from __future__ import annotations

import time

import httpx

from kubesage.journal import log


class PromError(RuntimeError):
    pass


class PromClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.calls: list[dict] = []  # every request as sent, for the run's trace.json

    def _get(self, path: str, params: dict) -> list[dict]:
        started = time.perf_counter()
        call = {"kind": "promql", "stage": 1, "request": {"method": "GET", "url": self.base_url + path, "params": params}}
        self.calls.append(call)
        try:
            resp = self._http.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            call["response"] = {"error": str(exc)}
            raise PromError(f"{path} failed: {exc}") from exc
        finally:
            call["latency_ms"] = int((time.perf_counter() - started) * 1000)
        body = resp.json()
        if body.get("status") != "success":
            call["response"] = {"status": resp.status_code, "error": body.get("error", str(body))}
            raise PromError(body.get("error", str(body)))
        result = body["data"]["result"]
        sample = result[0] if result else None
        call["response"] = {"status": resp.status_code, "series": len(result), "first_series": sample and {
            "metric": sample.get("metric"), "values": (sample.get("values") or [sample.get("value")])[:3]}}
        log("promql", f"{path.rsplit('/', 1)[-1]} {params['query'][:90]} -> {len(result)} series in {call['latency_ms']} ms",
            stage=1)
        return result

    def query(self, promql: str, at: float | None = None) -> list[dict]:
        params = {"query": promql}
        if at is not None:
            params["time"] = at
        return self._get("/api/v1/query", params)

    def query_range(self, promql: str, start: float, end: float, step: int) -> list[dict]:
        return self._get("/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step})
