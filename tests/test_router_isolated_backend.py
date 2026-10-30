"""AgentRouter.create_isolated_backend — fresh non-cached backends per call."""

import pytest

from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings


class TestCreateIsolatedBackend:
    """create_isolated_backend builds a fresh backend instance with optional
    settings overrides — used for short-lived specialist runs."""

    def test_returns_fresh_instance(self):
        a = AgentRouter.create_isolated_backend("deep_agents", Settings())
        b = AgentRouter.create_isolated_backend("deep_agents", Settings())
        assert a is not b

    def test_applies_model_override(self):
        backend = AgentRouter.create_isolated_backend(
            "deep_agents",
            Settings(deep_agents_model="anthropic:claude-sonnet-4-6"),
            settings_override={"deep_agents_model": "openai_compatible:deepseek-v4-pro"},
        )
        assert backend.settings.deep_agents_model == "openai_compatible:deepseek-v4-pro"

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="not registered"):
            AgentRouter.create_isolated_backend("nonexistent_backend", Settings())
