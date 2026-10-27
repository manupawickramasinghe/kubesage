"""Minimal Prometheus HTTP API client. Works against Prometheus and VictoriaMetrics alike."""
from __future__ import annotations

import httpx


class PromError(RuntimeError):
    pass


class PromClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def _get(self, path: str, params: dict) -> list[dict]:
        try:
            resp = self._http.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PromError(f"{path} failed: {exc}") from exc
        body = resp.json()
        if body.get("status") != "success":
            raise PromError(body.get("error", str(body)))
        return body["data"]["result"]

    def query(self, promql: str, at: float | None = None) -> list[dict]:
        params = {"query": promql}
        if at is not None:
            params["time"] = at
        return self._get("/api/v1/query", params)

    def query_range(self, promql: str, start: float, end: float, step: int) -> list[dict]:
        return self._get("/api/v1/query_range", {"query": promql, "start": start, "end": end, "step": step})
