"""Pocket-specialist settings — defaults, env var resolution, model fallback."""

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
