"""Tests for soul fields on AgentConfig and creation schemas."""

from __future__ import annotations

from ee.cloud.agents.schemas import CreateAgentRequest
from ee.cloud.models.agent import AgentConfig


def test_agent_config_soul_defaults():
    config = AgentConfig()
    assert config.soul_enabled is True
    assert config.soul_persona == ""
    assert config.soul_archetype == ""
    assert config.soul_values == ["helpfulness", "accuracy"]
    assert "openness" in config.soul_ocean
    assert config.soul_ocean["conscientiousness"] == 0.85


def test_agent_config_with_persona():
    config = AgentConfig(
        soul_persona="You are a sharp CFO who speaks in numbers",
        backend="claude_agent_sdk",
        model="",
    )
    assert config.soul_persona == "You are a sharp CFO who speaks in numbers"
    assert config.model == ""


def test_agent_config_custom_ocean():
    config = AgentConfig(
        soul_ocean={
            "openness": 0.9,
            "conscientiousness": 0.5,
            "extraversion": 0.8,
            "agreeableness": 0.3,
            "neuroticism": 0.1,
        }
    )
    assert config.soul_ocean["extraversion"] == 0.8


def test_agent_config_no_soul_path():
    config = AgentConfig()
    assert not hasattr(config, "soul_path")


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_create_agent_with_persona():
    req = CreateAgentRequest(
        name="CFO",
        slug="cfo",
        persona="Sharp financial advisor",
        backend="claude_agent_sdk",
    )
    assert req.persona == "Sharp financial advisor"
    assert req.backend == "claude_agent_sdk"
    assert req.model == ""


def test_create_agent_with_soul_customization():
    req = CreateAgentRequest(
        name="CFO",
        slug="cfo",
        persona="CFO persona",
        soul_ocean={
            "openness": 0.5,
            "conscientiousness": 0.9,
            "extraversion": 0.3,
            "agreeableness": 0.7,
            "neuroticism": 0.1,
        },
        soul_values=["accuracy", "brevity"],
    )
    assert req.soul_ocean["conscientiousness"] == 0.9
    assert req.soul_values == ["accuracy", "brevity"]
