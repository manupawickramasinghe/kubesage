"""OpenAI-compatible chat client with per-call token and cost accounting.

mode="mock" swaps the network call for a deterministic stand-in and *estimates* tokens, so the pipeline
and its cost telemetry can be demoed without an API key. Mock entries are always labelled as such.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from kubesage.config import ModelsConfig
from kubesage.journal import log

T = TypeVar("T", bound=BaseModel)

CACHE_MIN_TOKENS = 1024  # typical minimum prefix length for provider prompt caching


class LLMError(RuntimeError):
    pass


class LedgerEntry(BaseModel):
    stage: str
    workload: str
    tier: str
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    mock: bool


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _strip_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return match.group(1) if match else text


class LLMClient:
    def __init__(self, models: ModelsConfig, mode: str):
        self.models, self.mode = models, mode
        self._seen_prefixes: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._http = None
        self.trace: list[dict] = []  # every exchange, for the run's trace.json; the key appears only as its env var
        if mode == "live":
            key = os.environ.get(models.provider.api_key_env)
            if not key:
                raise LLMError(f"live mode needs ${models.provider.api_key_env}")
            self._http = httpx.Client(base_url=models.provider.base_url.rstrip("/"), timeout=models.provider.timeout_s,
                                      headers={"Authorization": f"Bearer {key}"})

    def structured(self, tier: str, system: str, user: str, schema: type[T], mock: Callable[[], T], *,
                   stage: str, workload: str, unit: str = "router", flow_stage: int | None = None) -> tuple[T, LedgerEntry]:
        cfg = self.models.tiers[tier]
        started = time.perf_counter()
        if self.mode == "live":
            result, (inp, cached, out), exchanges = self._live(tier, system, user, schema)
        else:
            result = mock()
            inp = estimate_tokens(system) + estimate_tokens(user)
            cached = self._mock_cached(tier, system)
            out = estimate_tokens(result.model_dump_json())
            exchanges = [{"request": self._request(self._body(tier, [{"role": "system", "content": system},
                                                                    {"role": "user", "content": user}])),
                          "response": {"note": "mock mode: not sent; deterministic stand-in",
                                       "body": json.loads(result.model_dump_json())}, "sent": False}]
        entry = LedgerEntry(stage=stage, workload=workload, tier=tier, model=cfg.model, input_tokens=inp,
                            cached_tokens=cached, output_tokens=out, cost_usd=cfg.cost(inp, cached, out),
                            latency_ms=int((time.perf_counter() - started) * 1000), mock=self.mode != "live")
        flow_stage = flow_stage or (2 if stage == "triage" else 3)
        with self._lock:
            for attempt, exchange in enumerate(exchanges, 1):
                self.trace.append({"kind": "llm", "stage": flow_stage, "llm_stage": stage,
                                   "workload": workload, "tier": tier, "label": cfg.label or tier, "model": cfg.model,
                                   "attempt": attempt, **exchange})
        log(unit, f"{stage} {workload} on {cfg.label or tier} ({cfg.model}{', mock' if entry.mock else ''}): "
                  f"{inp} in{f' ({cached} cached)' if cached else ''} / {out} out tokens, ${entry.cost_usd:.4f}, "
                  f"{entry.latency_ms} ms", stage=flow_stage)
        return result, entry

    def _body(self, tier: str, messages: list[dict]) -> dict:
        cfg = self.models.tiers[tier]
        return {"model": cfg.model, "messages": messages, "temperature": cfg.temperature,
                "max_tokens": cfg.max_output_tokens, "response_format": {"type": "json_object"}, **cfg.extra_body}

    def _request(self, body: dict) -> dict:
        provider = self.models.provider
        return {"method": "POST", "url": provider.base_url.rstrip("/") + "/chat/completions",
                "headers": {"Authorization": f"Bearer ${provider.api_key_env}", "Content-Type": "application/json"},
                "body": body}

    def _mock_cached(self, tier: str, system: str) -> int:
        """Pretend the provider caches a repeated system prompt, the way real prefix caching would."""
        tokens = estimate_tokens(system)
        if self.models.tiers[tier].cached_input_per_mtok is None or tokens < CACHE_MIN_TOKENS:
            return 0
        key = (tier, hashlib.sha256(system.encode()).hexdigest())
        with self._lock:
            hit = key in self._seen_prefixes
            self._seen_prefixes.add(key)
        return tokens if hit else 0

    def _post(self, body: dict, attempts: int = 4) -> dict:
        """POST /chat/completions, retrying rate limits and 5xx with backoff (honours Retry-After)."""
        for attempt in range(attempts):
            try:
                resp = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                raise LLMError(f"{body['model']}: {exc}") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(float(resp.headers.get("retry-after") or 2 ** attempt))
                    continue
            if resp.is_error:
                raise LLMError(f"{body['model']}: HTTP {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise AssertionError("unreachable")

    def _live(self, tier: str, system: str, user: str, schema: type[T]) -> tuple[T, tuple[int, int, int], list[dict]]:
        cfg = self.models.tiers[tier]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        inp = cached = out = 0
        exchanges = []
        problem = ""
        for _ in range(2):  # one retry: with the validation error fed back, or with less reasoning
            body = self._body(tier, list(messages))
            if problem.startswith("no answer") and body.get("reasoning_effort") == "high":
                body["reasoning_effort"] = "medium"  # it spent the whole budget reasoning; leave room for the answer
            started = time.perf_counter()
            data = self._post(body)
            exchanges.append({"request": self._request(body), "response": {"status": 200, "body": data},
                              "latency_ms": int((time.perf_counter() - started) * 1000), "sent": True})
            usage = data.get("usage") or {}
            inp += usage.get("prompt_tokens", 0)
            out += usage.get("completion_tokens", 0)
            cached += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            if not content.strip():  # gpt-oss can reason until max_tokens and send no content key at all
                problem = (f"no answer (finish_reason {choice.get('finish_reason')!r} after "
                           f"{usage.get('completion_tokens', 0)} output tokens)")
                continue
            try:
                return schema.model_validate_json(_strip_fences(content)), (inp, cached, out), exchanges
            except ValidationError as exc:
                problem = "invalid JSON"
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That JSON failed validation:\n{exc}\nReply with corrected JSON only."}]
        raise LLMError(f"{cfg.model} failed twice for {schema.__name__}: {problem}")
