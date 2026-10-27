"""Typed loaders for config/models.yaml and config/skus.yaml."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_DIR = Path(os.environ.get("KUBESAGE_CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))


class TierConfig(BaseModel):
    model: str
    label: str = ""  # short display name for the tier, e.g. "Spark"; defaults to the tier key
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    max_output_tokens: int = 2000
    temperature: float = 0.1
    extra_body: dict = Field(default_factory=dict)

    def cost(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
        cached_rate = self.input_per_mtok if self.cached_input_per_mtok is None else self.cached_input_per_mtok
        fresh = max(input_tokens - cached_tokens, 0)
        return (fresh * self.input_per_mtok + cached_tokens * cached_rate + output_tokens * self.output_per_mtok) / 1e6


class ProviderConfig(BaseModel):
    base_url: str
    api_key_env: str = "MODEL_API_KEY"
    timeout_s: float = 120


class ModelsConfig(BaseModel):
    provider: ProviderConfig
    tiers: dict[str, TierConfig]


class Sku(BaseModel):
    name: str
    family: str
    vcpu: int
    memory_gib: float
    hourly_usd: float

    @property
    def burstable(self) -> bool:
        return self.family == "burstable"


class NodePool(BaseModel):
    sku: str
    nodes: int


class SkuCatalog(BaseModel):
    region: str
    hours_per_month: float = 730
    pricing_reference: str
    cpu_to_gib_cost_ratio: float = 7.5
    allocatable_cpu: float = 0.9
    allocatable_mem: float = 0.8
    min_nodes: int = 2
    current_pool: NodePool
    node_label: str = "kubesage.io/sku"
    skus: list[Sku]

    def get(self, name: str) -> Sku:
        for sku in self.skus:
            if sku.name == name:
                return sku
        raise KeyError(f"unknown SKU {name!r}")

    def unit_prices(self) -> tuple[float, float]:
        """(USD per core-hour, USD per GiB-hour) derived from the reference SKU."""
        ref = self.get(self.pricing_reference)
        core = ref.hourly_usd / (ref.vcpu + ref.memory_gib / self.cpu_to_gib_cost_ratio)
        return core, core / self.cpu_to_gib_cost_ratio


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text())


def load_models(profile: str | None = None) -> ModelsConfig:
    """config/models.yaml (Meta) by default; KUBESAGE_MODELS=<name> selects config/models.<name>.yaml."""
    profile = profile or os.environ.get("KUBESAGE_MODELS")
    return ModelsConfig.model_validate(_load(f"models.{profile}.yaml" if profile else "models.yaml"))


def load_skus() -> SkuCatalog:
    return SkuCatalog.model_validate(_load("skus.yaml"))
