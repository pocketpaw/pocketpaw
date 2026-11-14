# tests/test_claude_sdk_agent_model.py — an agent's own model reaches the Claude
# SDK options, whatever the process default, provider route or smart routing says.
#
# Drives the real seam end to end: ``AgentPool._build`` clones the process
# settings and routes the agent's ``config.model`` onto them, then the real
# ``ClaudeAgentSDK._build_options`` (with the real ``resolve_llm_client``) picks
# the model the CLI is launched with. Precedence pinned here: per-send override >
# agent ``config.model`` > process default / smart routing. An agent with no
# model keeps the process default and smart routing exactly as before.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.config import Settings

pytestmark = pytest.mark.asyncio

_DEFAULT = "claude-haiku-5-5"  # the process-wide default (Infisical in prod)
_AGENT = "claude-sonnet-5-5"  # what the owner PATCHed onto the agent


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


class _Capture:
    settings = None

    def __init__(self, settings, **_kwargs) -> None:
        type(self).settings = settings

    async def stop(self) -> None:  # pragma: no cover
        pass


def _process_settings(**overrides) -> Settings:
    base = {
        "agent_backend": "claude_agent_sdk",
        "claude_sdk_model": _DEFAULT,
        "anthropic_api_key": "sk-ant-test",
        "litellm_api_key": "sk-litellm-test",
        "litellm_api_base": "http://gateway.test",
        "litellm_model": _DEFAULT,
        "model_tier_simple": _DEFAULT,
        "sdk_load_bundled_skills": False,
    }
    base.update(overrides)
    return Settings(**base)


async def _agent_settings(monkeypatch, process: Settings, model: str) -> Settings:
    """The per-agent Settings the real ``AgentPool._build`` hands the backend."""
    from pocketpaw import config as config_mod
    from pocketpaw.agents import pool as pool_mod
    from pocketpaw.agents import registry as registry_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: process)
    monkeypatch.setattr(registry_mod, "get_backend_class", lambda _name: _Capture)
    cfg = {"backend": "claude_agent_sdk", "model": model, "soul_enabled": False, "tools": []}
    doc = SimpleNamespace(
        id="64b7f0000000000000000001",
        name="Ripple",
        slug="ripple",
        workspace="ws-test",
        updatedAt=datetime.now(UTC),
        config=SimpleNamespace(model_dump=lambda: dict(cfg)),
    )
    await pool_mod.AgentPool()._build(doc)
    assert _Capture.settings is not None
    return _Capture.settings


async def _sdk_model(settings: Settings, message: str = "hi", **kwargs) -> str | None:
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(settings)
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _Options
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    with patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}):
        built = await sdk._build_options(
            message,
            system_prompt="identity",
            session_key="s1",
            deny_mcp_tool_ids=frozenset(),
            allow_sdk_tools=frozenset(),
            allow_mcp_tool_ids=None,
            skill_names=frozenset(),
            stderr_sink=[],
            **kwargs,
        )
    return built.options_kwargs.get("model")


# --- the bug: the agent's model lost to the process default -----------------


async def test_agent_model_wins_on_the_litellm_route(monkeypatch):
    """Claude SDK through the LiteLLM gateway: the agent's model must be launched,
    not the gateway default the provider adapter resolves."""
    process = _process_settings(claude_sdk_provider="litellm")
    settings = await _agent_settings(monkeypatch, process, _AGENT)
    assert await _sdk_model(settings) == _AGENT


async def test_agent_model_wins_over_smart_routing(monkeypatch):
    """Smart routing classifies "hi" as simple (haiku tier); a pinned agent model
    must still be the one that runs."""
    process = _process_settings(claude_sdk_provider="anthropic", smart_routing_enabled=True)
    settings = await _agent_settings(monkeypatch, process, _AGENT)
    assert await _sdk_model(settings) == _AGENT


async def test_per_send_override_beats_the_agent_model(monkeypatch):
    process = _process_settings(claude_sdk_provider="litellm", smart_routing_enabled=True)
    settings = await _agent_settings(monkeypatch, process, _AGENT)
    assert await _sdk_model(settings, model_override="claude-opus-5-5") == "claude-opus-5-5"


# --- agents with no model keep the process default, unchanged ----------------


async def test_no_agent_model_keeps_smart_routing(monkeypatch):
    process = _process_settings(claude_sdk_provider="anthropic", smart_routing_enabled=True)
    settings = await _agent_settings(monkeypatch, process, "")
    assert settings.smart_routing_enabled is True
    assert await _sdk_model(settings) == _DEFAULT


async def test_no_agent_model_keeps_the_process_default(monkeypatch):
    process = _process_settings(claude_sdk_provider="anthropic")
    settings = await _agent_settings(monkeypatch, process, "")
    assert await _sdk_model(settings) == _DEFAULT
