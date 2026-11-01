"""Judges run Meta Muse: the default profiles must only name models the Meta Model API serves.

Muse Glimmer is open-weight only and is NOT on api.meta.ai — a profile naming it 404s on the
first live call. Spark/Wonder IDs and Standard prices verified Oct 2026.
"""
import yaml

from kubesage.agents.loader import load_team
from kubesage.config import CONFIG_DIR, load_models

SERVED = {"muse-spark-1.3", "muse-wonder-1.0"}
PRICES = {  # (input, cached, output) USD per 1M tokens, Standard mode
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-wonder-1.0": (4.00, 0.20, 20.00),
}


def test_default_profile_is_meta_with_only_served_models():
    models = load_models()
    assert models.provider.base_url == "https://api.meta.ai/v1"
    assert models.provider.api_key_env == "MODEL_API_KEY"
    for tier in models.tiers.values():
        assert tier.model in SERVED, f"{tier.model} is not served on api.meta.ai"
        assert (tier.input_per_mtok, tier.cached_input_per_mtok, tier.output_per_mtok) == PRICES[tier.model]


def test_router_story_holds_spark_triages_wonder_strategizes():
    models = load_models()
    assert models.tiers["triage"].model == "muse-spark-1.3"
    assert models.tiers["worker"].model == "muse-spark-1.3"
    assert models.tiers["strategist"].model == "muse-wonder-1.0"
    # Routing must actually save: every Spark tier cheaper than Wonder on both axes.
    wonder = models.tiers["strategist"]
    for name in ("triage", "worker"):
        assert models.tiers[name].input_per_mtok < wonder.input_per_mtok
        assert models.tiers[name].output_per_mtok < wonder.output_per_mtok


def test_agent_team_meta_profile_uses_the_same_served_models():
    team = load_team(profile="meta")
    assert team.provider.base_url == "https://api.meta.ai/v1"
    for agent in team.agents:
        assert agent.tier.model in SERVED, f"{agent.name}: {agent.tier.model} not served on api.meta.ai"
    assert team.thinker.tier.model == "muse-wonder-1.0"  # large = Wonder


def test_no_profile_anywhere_names_glimmer_as_an_api_model():
    # Comments may explain why Glimmer is absent; model *fields* must never name it.
    for name in ("models.yaml", "models.cerebras.yaml"):
        doc = yaml.safe_load((CONFIG_DIR / name).read_text())
        assert all("glimmer" not in t["model"] for t in doc["tiers"].values()), name
    agents = yaml.safe_load(open("agents/models.yaml").read())
    assert all("glimmer" not in m["model"] for m in agents["profiles"]["meta"]["models"].values())
