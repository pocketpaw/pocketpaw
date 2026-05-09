"""Pocket-specialist settings — defaults, env var resolution, model fallback."""

from ee.agent.pocket_specialist.settings import resolve_specialist_model
from pocketpaw.config import Settings


class TestPocketSpecialistSettings:
    def test_defaults(self):
        s = Settings()
        assert s.pocket_specialist_backend == "deep_agents"
        assert s.pocket_specialist_model == ""
        assert s.pocket_specialist_max_validation_retries == 3

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_BACKEND", "claude_agent_sdk")
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_MODEL", "openai_compatible:deepseek-v4-pro")
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES", "5")
        s = Settings()
        assert s.pocket_specialist_backend == "claude_agent_sdk"
        assert s.pocket_specialist_model == "openai_compatible:deepseek-v4-pro"
        assert s.pocket_specialist_max_validation_retries == 5


class TestResolveSpecialistModel:
    def test_explicit_override_wins(self):
        s = Settings(
            pocket_specialist_backend="deep_agents",
            pocket_specialist_model="openai_compatible:deepseek-v4-pro",
            deep_agents_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "openai_compatible:deepseek-v4-pro"

    def test_falls_back_to_backend_default_when_unset(self):
        s = Settings(
            pocket_specialist_backend="deep_agents",
            deep_agents_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "anthropic:claude-sonnet-4-6"

    def test_returns_empty_when_backend_has_no_model_setting(self):
        # opencode has opencode_model; copilot_sdk has copilot_sdk_model;
        # if a backend has none, resolver returns "" — caller must handle.
        s = Settings(pocket_specialist_backend="not_a_real_backend")
        assert resolve_specialist_model(s) == ""

    def test_falls_back_to_claude_sdk_model_for_claude_agent_sdk_backend(self):
        # claude_agent_sdk's Settings field is claude_sdk_model (not
        # claude_agent_sdk_model). The resolver must remap so users who
        # leave pocket_specialist_model="" still inherit the configured
        # claude_sdk_model value.
        s = Settings(
            pocket_specialist_backend="claude_agent_sdk",
            claude_sdk_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "anthropic:claude-sonnet-4-6"
