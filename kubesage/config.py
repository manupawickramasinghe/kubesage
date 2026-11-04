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
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cached_input_per_mtok: float | None = Field(default=None, ge=0)
    max_output_tokens: int = Field(default=2000, ge=1)
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
    hourly_usd: float = Field(ge=0)
    local_nvme_gib: float | None = None
    planner: bool = True  # False: offered to the agent team, but not to the right-sizing node-pool planner

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


class CalendarEvent(BaseModel):
    """One operator-declared business event: a sale, holiday rush or working-hours window.

    This is the only source of future dates and traffic expectations. The LLM may reason about these
    events and the peaks found in metrics; it must never invent an event that is not declared here.
    """

    name: str
    kind: str = "other"  # working-hours | sale | holiday | promotion | other
    days: str | None = None  # weekly: cron day-of-week, Sunday=0 ("1-5", "0,6", "*")
    date: str | None = None  # one-off / yearly: "2026-11-27"
    repeat_yearly: bool = False
    start_date: str | None = None  # yearly range start, "MM-DD"
    end_date: str | None = None  # yearly range end, "MM-DD"
    start_time: str = "09:00"  # local "HH:MM"
    end_time: str = "17:00"
    traffic_multiplier: float = Field(default=1.0, gt=0)
    prewarm_minutes: int | None = Field(default=None, ge=0, le=240)
    cooldown_minutes: int = Field(default=0, ge=0, le=240)
    workloads: list[str] = Field(default_factory=lambda: ["*"])
    note: str = ""

    def model_post_init(self, __context) -> None:
        schedules = [self.days is not None, self.date is not None, self.start_date is not None]
        if sum(schedules) != 1:
            raise ValueError(f"calendar event {self.name!r} needs exactly one of days, date or start_date/end_date")
        if self.start_date is not None and self.end_date is None:
            raise ValueError(f"calendar event {self.name!r} needs end_date with start_date")
        if self.days is not None and not _valid_days(self.days):
            raise ValueError(f"calendar event {self.name!r} has bad days {self.days!r} "
                             "(cron day-of-week: '*', a day 0-6, a range like 1-5, or a list like 0,6)")
        for label, value in (("start_time", self.start_time), ("end_time", self.end_time)):
            hour, _, minute = value.partition(":")
            if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60):
                raise ValueError(f"calendar event {self.name!r} has bad {label} {value!r}")

    @property
    def weekly(self) -> bool:
        """Weekly events fit the KEDA cron subset; dated events are forecast-only (see forecast.py)."""
        return self.days is not None


def _valid_days(field: str) -> bool:
    """The KEDA cron day-of-week subset: '*', single days, ranges and lists, Sunday=0..Saturday=6."""
    if field == "*":
        return True
    for part in field.split(","):
        lo, dash, hi = part.partition("-")
        if not lo.isdigit() or (dash and not hi.isdigit()):
            return False
        if not (0 <= int(lo) <= (int(hi) if dash else int(lo)) <= 6):
            return False
    return True


class BusinessCalendar(BaseModel):
    prewarm_minutes: int = Field(default=15, ge=0, le=240)
    events: list[CalendarEvent] = Field(default_factory=list)

    def for_workload(self, name: str) -> list[CalendarEvent]:
        return [e for e in self.events if "*" in e.workloads or name in e.workloads]


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text())


def load_models(profile: str | None = None) -> ModelsConfig:
    """config/models.yaml (Meta) by default; KUBESAGE_MODELS=<name> selects config/models.<name>.yaml."""
    profile = profile or os.environ.get("KUBESAGE_MODELS")
    return ModelsConfig.model_validate(_load(f"models.{profile}.yaml" if profile else "models.yaml"))


def load_skus() -> SkuCatalog:
    return SkuCatalog.model_validate(_load("skus.yaml"))


def load_calendar() -> BusinessCalendar:
    """config/calendar.yaml; an absent file means no declared events (history-only forecasting)."""
    path = CONFIG_DIR / "calendar.yaml"
    if not path.exists():
        return BusinessCalendar()
    return BusinessCalendar.model_validate(yaml.safe_load(path.read_text()) or {})
