"""What each role must return. The engine validates every reply against these and against the data it was given."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["info", "warning", "critical"]
Category = Literal["memory", "cpu", "disk_io", "disk_space", "latency", "pod_count", "pod_density", "security",
                   "cost", "other"]
ActionType = Literal["set_resources", "change_node_sku", "set_node_pool", "autoscale", "code_change", "storage",
                     "spread_pods", "security_hardening", "remove_workload", "investigate"]


class Finding(BaseModel):
    title: str
    severity: Severity
    category: Category
    target: str = Field(description="what it is about, e.g. deployment/lab/ledger-cache or node/manupa-hnx")
    signal_ids: list[str] = Field(description="ids of the signals this finding rests on; at least one")
    summary: str = Field(description="one or two sentences, with the numbers")


class SubAgentReport(BaseModel):
    summary: str
    findings: list[Finding]


class Issue(BaseModel):
    id: str = Field(description="I1, I2, ...")
    title: str
    category: Category
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    root_cause: str
    evidence: list[str] = Field(description="signal ids")
    targets: list[str]


class Action(BaseModel):
    id: str = Field(description="A1, A2, ...")
    issue: str = Field(description="the Issue id this action fixes")
    type: ActionType
    target: str = Field(description="deployment/<namespace>/<name>, nodepool/<name> or namespace/<name>")
    summary: str = Field(description="the change, in one sentence")
    params: dict = Field(default_factory=dict)
    rationale: str
    risk: str = ""
    needs_human: bool = False


class Diagnosis(BaseModel):
    summary: str
    issues: list[Issue]
    actions: list[Action]


class Edit(BaseModel):
    path: str = Field(description="repo-relative, under gitops/")
    find: str = Field(description="exact text that occurs once in the file; empty to create a new file")
    replace: str
    action: str = Field(description="the Action id this edit implements")


class Skipped(BaseModel):
    action: str
    reason: str


class ChangeSet(BaseModel):
    title: str = Field(description="pull request title, under 72 characters")
    body: str = Field(description="pull request description, markdown")
    edits: list[Edit]
    skipped: list[Skipped] = Field(default_factory=list)
