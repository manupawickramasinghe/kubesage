"""Structured contracts between pipeline stages. LLMs return parameters; KubeSage renders the YAML."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["HEALTHY", "ROUTINE", "COMPLEX"]


class TriageItem(BaseModel):
    workload: str = Field(description="workload id exactly as given, e.g. demo/payment-service")
    status: Status
    signals: list[str] = Field(default_factory=list, description="short kebab-case evidence tags")
    summary: str = Field(description="one sentence")


class TriageReport(BaseModel):
    items: list[TriageItem]


class ResourceSpec(BaseModel):
    cpu_request_m: int = Field(ge=1, description="CPU request, millicores")
    cpu_limit_m: int | None = Field(default=None, ge=1, description="CPU limit, millicores; null removes the limit")
    mem_request_mi: int = Field(ge=1, description="memory request, MiB")
    mem_limit_mi: int = Field(ge=1, description="memory limit, MiB")


class CronWindow(BaseModel):
    start: str = Field(description='5-field cron "M H * * DOW" in the cluster timezone')
    end: str = Field(description='5-field cron "M H * * DOW" in the cluster timezone')
    desired_replicas: int = Field(ge=1)


class ScalingPlan(BaseModel):
    min_replicas: int = Field(ge=0, description="0 allows scale-to-zero outside cron windows")
    max_replicas: int = Field(ge=1)
    cron_windows: list[CronWindow] = Field(default_factory=list)
    cpu_utilization_target: int | None = Field(default=None, description="percent of CPU request; null for none")


class Recommendation(BaseModel):
    workload: str
    risk: Literal["low", "medium", "high"]
    diagnosis: str = Field(description="2-4 sentences an operator can act on, citing the numbers")
    resources: ResourceSpec | None = None
    scaling: ScalingPlan | None = None
    follow_ups: list[str] = Field(default_factory=list, description="engineering actions outside k8s config")


class SkuChoice(BaseModel):
    sku: str
    nodes: int = Field(ge=1)
    rationale: str
