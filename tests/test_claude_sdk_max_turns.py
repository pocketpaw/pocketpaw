# tests/test_claude_sdk_max_turns.py — a per-run raised tool-turn cap, at the pool
# seam and on the Claude SDK backend.
#
# A /sites app build (an approved PLAN.md, every milestone in one turn) can need
# more than ``claude_sdk_max_turns``; run_core asks for ``sites_app_build_max_turns``
# through the surface profile. Pins: the cap only ever RAISES the configured one
# (and never limits an unlimited one), it reaches ``options_kwargs["max_turns"]``,
# it is part of the warm-client key (the CLI fixes it at connect()), and the pool
# forwards it only to a backend that declares it. pydantic_ai's side (a free run
# stays at its bound) is pinned in tests/test_pydantic_ai_backend.py.
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.claude_sdk import (
    ClaudeAgentSDK,
    ClaudeSDKBackend,
    _effective_max_turns,
    _result_error_text,
)
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity
from pocketpaw.agents.pool import AgentPool


def test_a_request_only_raises_the_configured_cap() -> None:
    assert _effective_max_turns(100, None) == 100
    assert _effective_max_turns(100, 300) == 300
    # Never lowers it.
    assert _effective_max_turns(100, 20) == 100
    # 0 is unlimited, sent as None, and a request cannot limit it.
    assert _effective_max_turns(0, None) is None
    assert _effective_max_turns(0, 300) is None


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


def _sdk(max_turns: int) -> ClaudeAgentSDK:
    settings = MagicMock()
    for key, value in {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": max_turns,
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
    }.items():
        setattr(settings, key, value)
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(settings)
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _Options
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    return sdk


async def _build(sdk, **kwargs):
    selection = ModelSelection(
        complexity=TaskComplexity.MODERATE, model="claude-sonnet-4-5-20250929", reason="t"
    )
    with patch("pocketpaw.llm.client.resolve_llm_client") as resolve:
        llm = MagicMock()
        llm.is_ollama = llm.is_openai_compatible = llm.is_gemini = False
        llm.is_litellm = llm.is_openrouter = False
        llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        resolve.return_value = llm
        with (
            patch("pocketpaw.agents.model_router.ModelRouter") as router,
            patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}),
        ):
            router.return_value.classify.return_value = selection
            return await sdk._build_options(
                "hello",
                system_prompt="identity",
                session_key="s1",
                deny_mcp_tool_ids=frozenset(),
                allow_sdk_tools=frozenset(),
                allow_mcp_tool_ids=None,
                skill_names=frozenset(),
                stderr_sink=[],
                **kwargs,
            )


@pytest.mark.asyncio
async def test_the_raised_cap_reaches_the_sdk_options_and_the_warm_key() -> None:
    sdk = _sdk(100)
    plain = await _build(sdk)
    raised = await _build(sdk, max_turns=300)
    assert plain.options_kwargs["max_turns"] == 100
    assert raised.options_kwargs["max_turns"] == 300

    # max_turns is fixed at connect(): a client built at 100 must not serve a 300 turn.
    def key(options):
        return sdk._client_cache_key(
            options, session_key="s1", turn_cap=sdk._raised_turn_cap(options)
        )

    assert key(plain.options) != key(raised.options)
    # An ordinary turn keys exactly as before the raise existed, so warm reuse holds.
    assert key(plain.options) == sdk._client_cache_key(plain.options, session_key="s1")
    # A "raise" to the configured value is no raise.
    same = await _build(sdk, max_turns=100)
    assert key(same.options) == key(plain.options)


def test_the_backend_run_and_prewarm_declare_it() -> None:
    import inspect

    assert "max_turns" in inspect.signature(ClaudeSDKBackend.run).parameters
    assert "max_turns" in inspect.signature(ClaudeSDKBackend.prewarm).parameters


def test_a_max_turns_stop_still_names_the_limit() -> None:
    event = SimpleNamespace(result=None, subtype="error_max_turns", terminal_reason=None)
    assert "tool-turn limit (300)" in _result_error_text(event, max_turns=300)


class _NarrowBackend:
    """No ``max_turns`` and no ``**kwargs``: the backends that keep their own cap."""

    def __init__(self) -> None:
        self.called = False

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[object]:
        self.called = True
        return
        yield  # pragma: no cover


class _WideBackend:
    def __init__(self) -> None:
        self.seen: object = "unset"

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list | None = None,
        session_key: str | None = None,
        max_turns: int | None = None,
    ) -> AsyncIterator[object]:
        self.seen = max_turns
        return
        yield  # pragma: no cover


async def _drive(monkeypatch, backend, **run_kwargs) -> None:
    pool = AgentPool()
    inst = SimpleNamespace(
        backend=backend,
        soul_manager=None,
        config={"soul_persona": "P", "system_prompt": ""},
        last_active=datetime.now(UTC),
        active_runs=0,
    )

    async def _fake_get(agent_id):
        return inst

    monkeypatch.setattr(pool, "get", _fake_get)
    async for _ in pool.run("a1", "hello", "session:s1", **run_kwargs):
        pass


@pytest.mark.asyncio
async def test_the_pool_forwards_the_cap_to_a_backend_that_declares_it(monkeypatch) -> None:
    backend = _WideBackend()
    await _drive(monkeypatch, backend, max_turns=300)
    assert backend.seen == 300


@pytest.mark.asyncio
async def test_the_pool_withholds_the_cap_when_unset(monkeypatch) -> None:
    backend = _WideBackend()
    await _drive(monkeypatch, backend)
    assert backend.seen is None


@pytest.mark.asyncio
async def test_a_backend_without_the_param_keeps_its_own_cap(monkeypatch) -> None:
    """A backend that does not declare it: no TypeError, it keeps its own cap."""
    backend = _NarrowBackend()
    await _drive(monkeypatch, backend, max_turns=300)
    assert backend.called
