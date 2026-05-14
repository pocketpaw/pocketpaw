"""Pocket-specialist settings — defaults, env var resolution, model fallback.

Every ``Settings(...)`` call passes ``_env_file=None`` so the in-code
defaults are the source of truth here. Without that, pydantic-settings
loads ``backend/.env`` (which most contributors keep populated with
``POCKETPAW_POCKET_SPECIALIST_BACKEND`` etc. for local dev) and the
tests measure the operator's local config instead of the code.
The autouse fixture clears any ``POCKETPAW_POCKET_SPECIALIST_*`` /
``POCKETPAW_DEEP_AGENTS_MODEL`` / ``POCKETPAW_CLAUDE_SDK_MODEL``
env vars that might be exported in the calling shell.
"""

import pytest

from ee.agent.pocket_specialist.settings import resolve_specialist_model
from pocketpaw.config import Settings

_LEAKY_ENV_KEYS = (
    "POCKETPAW_POCKET_SPECIALIST_BACKEND",
    "POCKETPAW_POCKET_SPECIALIST_MODEL",
    "POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES",
    "POCKETPAW_DEEP_AGENTS_MODEL",
    "POCKETPAW_CLAUDE_SDK_MODEL",
    "POCKETPAW_LANGCHAIN_REACT_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every env var these tests care about so a shell-exported
    override doesn't smuggle in alongside the kwargs."""
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestPocketSpecialistSettings:
    def test_defaults(self):
        s = Settings(_env_file=None)
        assert s.pocket_specialist_backend == "deep_agents"
        # The specialist emits structured rippleSpec JSON from a stable
        # ~12k-token design-rules prompt — Haiku handles that workload at
        # roughly 2-4x Sonnet's wall-time without measurable quality loss.
        # Operators wanting creative liberty (Sonnet) or cheap self-hosted
        # inference (DeepSeek) can override.
        assert s.pocket_specialist_model == "anthropic:claude-haiku-4-5-20251001"
        assert s.pocket_specialist_max_validation_retries == 3

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_BACKEND", "claude_agent_sdk")
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_MODEL", "openai_compatible:deepseek-v4-pro")
        monkeypatch.setenv("POCKETPAW_POCKET_SPECIALIST_MAX_VALIDATION_RETRIES", "5")
        s = Settings(_env_file=None)
        assert s.pocket_specialist_backend == "claude_agent_sdk"
        assert s.pocket_specialist_model == "openai_compatible:deepseek-v4-pro"
        assert s.pocket_specialist_max_validation_retries == 5


class TestResolveSpecialistModel:
    def test_explicit_override_wins(self):
        s = Settings(
            _env_file=None,
            pocket_specialist_backend="deep_agents",
            pocket_specialist_model="openai_compatible:deepseek-v4-pro",
            deep_agents_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "openai_compatible:deepseek-v4-pro"

    def test_falls_back_to_backend_default_when_unset(self):
        # ``pocket_specialist_model=""`` opts out of the Haiku default and
        # falls through to whatever the chosen backend's ``*_model``
        # setting is. Operators who want to track a single project-wide
        # model (e.g., always Sonnet) set both the backend's model and
        # blank the specialist override.
        s = Settings(
            _env_file=None,
            pocket_specialist_backend="deep_agents",
            pocket_specialist_model="",
            deep_agents_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "anthropic:claude-sonnet-4-6"

    def test_returns_empty_when_backend_has_no_model_setting(self):
        # opencode has opencode_model; copilot_sdk has copilot_sdk_model;
        # if a backend has none, resolver returns "" — caller must handle.
        # Pin pocket_specialist_model="" so the Haiku default doesn't
        # short-circuit the fallback path being tested.
        s = Settings(
            _env_file=None,
            pocket_specialist_backend="not_a_real_backend",
            pocket_specialist_model="",
        )
        assert resolve_specialist_model(s) == ""

    def test_falls_back_to_claude_sdk_model_for_claude_agent_sdk_backend(self):
        # claude_agent_sdk's Settings field is claude_sdk_model (not
        # claude_agent_sdk_model). The resolver must remap so users who
        # leave pocket_specialist_model="" still inherit the configured
        # claude_sdk_model value.
        s = Settings(
            _env_file=None,
            pocket_specialist_backend="claude_agent_sdk",
            pocket_specialist_model="",
            claude_sdk_model="anthropic:claude-sonnet-4-6",
        )
        assert resolve_specialist_model(s) == "anthropic:claude-sonnet-4-6"
