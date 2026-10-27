"""OpenAI-compatible chat client with per-call token and cost accounting.

mode="mock" swaps the network call for a deterministic stand-in and *estimates* tokens, so the pipeline
and its cost telemetry can be demoed without an API key. Mock entries are always labelled as such.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from kubesage.config import ModelsConfig

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
        if mode == "live":
            key = os.environ.get(models.provider.api_key_env)
            if not key:
                raise LLMError(f"live mode needs ${models.provider.api_key_env}")
            self._http = httpx.Client(base_url=models.provider.base_url.rstrip("/"), timeout=models.provider.timeout_s,
                                      headers={"Authorization": f"Bearer {key}"})

    def structured(self, tier: str, system: str, user: str, schema: type[T], mock: Callable[[], T], *,
                   stage: str, workload: str) -> tuple[T, LedgerEntry]:
        cfg = self.models.tiers[tier]
        started = time.perf_counter()
        if self.mode == "live":
            result, (inp, cached, out) = self._live(tier, system, user, schema)
        else:
            result = mock()
            inp = estimate_tokens(system) + estimate_tokens(user)
            cached = self._mock_cached(tier, system)
            out = estimate_tokens(result.model_dump_json())
        entry = LedgerEntry(stage=stage, workload=workload, tier=tier, model=cfg.model, input_tokens=inp,
                            cached_tokens=cached, output_tokens=out, cost_usd=cfg.cost(inp, cached, out),
                            latency_ms=int((time.perf_counter() - started) * 1000), mock=self.mode != "live")
        return result, entry

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

    def _live(self, tier: str, system: str, user: str, schema: type[T]) -> tuple[T, tuple[int, int, int]]:
        cfg = self.models.tiers[tier]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        inp = cached = out = 0
        for _ in range(2):  # one retry with the validation error fed back
            body = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature,
                    "max_tokens": cfg.max_output_tokens, "response_format": {"type": "json_object"}, **cfg.extra_body}
            try:
                resp = self._http.post("/chat/completions", json=body)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise LLMError(f"{cfg.model}: {exc}") from exc
            data = resp.json()
            usage = data.get("usage") or {}
            inp += usage.get("prompt_tokens", 0)
            out += usage.get("completion_tokens", 0)
            cached += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            content = data["choices"][0]["message"]["content"] or ""
            try:
                return schema.model_validate_json(_strip_fences(content)), (inp, cached, out)
            except ValidationError as exc:
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That JSON failed validation:\n{exc}\nReply with corrected JSON only."}]
        raise LLMError(f"{cfg.model} returned invalid JSON twice for {schema.__name__}")
