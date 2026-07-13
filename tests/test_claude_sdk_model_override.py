# tests/test_claude_sdk_model_override.py
# Created: 2026-07-08 (CS-13, feat/per-send-model-override) — pins the backend
# half of the per-send model picker. ``ClaudeSDKBackend._build_options`` grows an
# optional ``model_override`` that, when set, is applied as the LAST word in the
# model-selection block, so it wins over ALL of: smart-routing's complexity pick,
# the configured ``claude_sdk_model``, and the auto-select default. These tests
# drive ``_build_options`` directly (the seam the token-usage event and the warm
# cache key both read ``options_kwargs["model"]`` from) under the fake-SDK harness
# reused from tests/test_claude_sdk_prewarm.py, and assert:
#   1. an override lands on ``options_kwargs["model"]`` verbatim;
#   2. the override WINS over a configured ``claude_sdk_model``;
#   3. the override WINS over smart-routing;
#   4. ``None`` (older clients / no picker) leaves selection byte-identical to
#      today (auto-select → no model set; or the configured model untouched).

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"


class _Options:
    """Real-enough options object: reads ``model`` / ``allowed_tools`` /
    ``system_prompt`` / ``plugins`` the way ``_build_options`` sets them."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


def _make_settings(**overrides):
    defaults = {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": None,
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _make_sdk(settings=None):
    s = settings or _make_settings()
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(s)
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _Options
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    return sdk


async def _build(sdk, *, message="hello", model_override=None):
    """Drive ``_build_options`` to completion under the standard LLM/router/MCP
    patches and return the raw ``options_kwargs`` the turn will launch with."""
    selection = ModelSelection(
        complexity=TaskComplexity.MODERATE,
        model="claude-sonnet-4-5-20250929",
        reason="test",
    )
    with patch(_LLM_CLIENT) as mock_resolve:
        mock_llm = MagicMock()
        mock_llm.is_ollama = False
        mock_llm.is_openai_compatible = False
        mock_llm.is_gemini = False
        mock_llm.is_litellm = False
        mock_llm.is_openrouter = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm
        with (
            patch(_MODEL_ROUTER) as MockRouter,
            patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}),
        ):
            MockRouter.return_value.classify.return_value = selection
            built = await sdk._build_options(
                message,
                system_prompt="identity",
                history=None,
                session_key="s1",
                deny_mcp_tool_ids=frozenset(),
                allow_sdk_tools=frozenset(),
                allow_mcp_tool_ids=None,
                skill_names=frozenset(),
                stderr_sink=[],
                model_override=model_override,
            )
    return built.options_kwargs


@pytest.mark.asyncio
async def test_override_sets_options_model_verbatim():
    """A per-send override lands on ``options_kwargs['model']`` byte-for-byte."""
    sdk = _make_sdk()
    kwargs = await _build(sdk, model_override="claude-haiku-4-5-20251001")
    assert kwargs["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_override_wins_over_claude_sdk_model_setting():
    """The override beats a configured ``claude_sdk_model`` — it's the user's
    explicit choice for this one turn."""
    sdk = _make_sdk(_make_settings(claude_sdk_model="claude-opus-4-8"))
    # Sanity: without an override the configured model is what would be used.
    baseline = await _build(sdk, model_override=None)
    assert baseline["model"] == "claude-opus-4-8"
    # With an override, it wins.
    kwargs = await _build(sdk, model_override="claude-haiku-4-5-20251001")
    assert kwargs["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_override_wins_over_smart_routing():
    """The override beats smart-routing's complexity-based pick."""
    sdk = _make_sdk(_make_settings(smart_routing_enabled=True))
    # Sanity: routing alone would pick the router's model.
    baseline = await _build(sdk, model_override=None)
    assert baseline["model"] == "claude-sonnet-4-5-20250929"
    # The override supersedes it.
    kwargs = await _build(sdk, model_override="claude-haiku-4-5-20251001")
    assert kwargs["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_no_override_is_byte_identical_auto_select():
    """``None`` (older clients) leaves auto-select untouched — no model set."""
    sdk = _make_sdk()  # smart routing off, claude_sdk_model None → auto-select
    kwargs = await _build(sdk, model_override=None)
    assert "model" not in kwargs, "auto-select must not stamp a model when no override"
