"""Load the team from agents/: every folder with an agent.yaml is an agent; models.yaml maps sizes to models;
rules/ holds the custom rule sets in plain text."""
from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from kubesage.config import ModelsConfig, ProviderConfig, TierConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def agents_dir() -> Path:
    return Path(os.environ.get("KUBESAGE_AGENTS_DIR", REPO_ROOT / "agents"))


class AgentConfig(BaseModel):
    name: str
    role: str = Field(pattern="^(subagent|thinker|executor)$")
    model: str = "small"
    title: str = ""
    description: str = ""
    enabled: bool = True
    order: int = 100
    max_findings: int = 8
    settings: dict = Field(default_factory=dict)
    overrides: dict = Field(default_factory=dict)  # merged over the size's model entry, e.g. {extra_body: {...}}


@dataclass
class Agent:
    config: AgentConfig
    dir: Path
    prompt: str
    tier: TierConfig
    collect: Callable | None = None

    @property
    def name(self) -> str:
        return self.config.name


@dataclass
class Team:
    profile: str
    provider: ProviderConfig
    subagents: list[Agent]
    thinker: Agent
    executor: Agent
    rules: list[tuple[str, str]] = field(default_factory=list)

    @property
    def agents(self) -> list[Agent]:
        return [*self.subagents, self.thinker, self.executor]

    def models(self) -> ModelsConfig:
        """One tier per agent, so the LLM ledger and trace name the agent that made each call."""
        return ModelsConfig(provider=self.provider, tiers={a.name: a.tier for a in self.agents})

    def rules_text(self) -> str:
        return "\n\n".join(f"## {name}\n{text.strip()}" for name, text in self.rules if text.strip())


class TeamError(RuntimeError):
    pass


def _collector(path: Path) -> Callable:
    spec = importlib.util.spec_from_file_location(f"kubesage_agent_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "collect", None)):
        raise TeamError(f"{path} has no collect(ctx) function")
    return module.collect


def load_team(root: Path | None = None, profile: str | None = None) -> Team:
    root = Path(root or agents_dir())
    models = yaml.safe_load((root / "models.yaml").read_text())
    profile = profile or os.environ.get("KUBESAGE_MODELS") or models.get("default_profile", "meta")
    if profile not in models["profiles"]:
        raise TeamError(f"agents/models.yaml has no profile {profile!r}")
    prof = models["profiles"][profile]
    provider = ProviderConfig.model_validate(prof["provider"])
    agents: list[Agent] = []
    for cfg_path in sorted(root.glob("*/agent.yaml")):
        cfg = AgentConfig.model_validate(yaml.safe_load(cfg_path.read_text()))
        if not cfg.enabled:
            continue
        if cfg.model not in prof["models"]:
            raise TeamError(f"{cfg_path}: model {cfg.model!r} is not a size in profile {profile!r}")
        entry = {**prof["models"][cfg.model], **cfg.overrides}
        prompt_path = cfg_path.parent / "prompt.md"
        collect_path = cfg_path.parent / "collect.py"
        agents.append(Agent(config=cfg, dir=cfg_path.parent, prompt=prompt_path.read_text() if prompt_path.exists() else "",
                            tier=TierConfig.model_validate(entry),
                            collect=_collector(collect_path) if collect_path.exists() else None))
    subagents = sorted((a for a in agents if a.config.role == "subagent"), key=lambda a: (a.config.order, a.name))
    thinkers = [a for a in agents if a.config.role == "thinker"]
    executors = [a for a in agents if a.config.role == "executor"]
    if len(thinkers) != 1 or len(executors) != 1:
        raise TeamError("agents/ needs exactly one enabled thinker and one enabled executor")
    for a in subagents:
        if a.collect is None:
            raise TeamError(f"sub-agent {a.name} needs a collect.py")
    rules = [(p.name, p.read_text()) for p in sorted((root / "rules").glob("*")) if p.is_file()
             and p.suffix in (".txt", ".md") and not p.name.startswith((".", "_")) and p.name != "README.md"]
    return Team(profile=profile, provider=provider, subagents=subagents, thinker=thinkers[0], executor=executors[0],
                rules=rules)
