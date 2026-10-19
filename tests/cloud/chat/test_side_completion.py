# tests/cloud/chat/test_side_completion.py — ``chat.side_completion.agent_complete``.
#
# One tool-less completion on the workspace agent's own backend and model. The
# agent pool is a fake (an async generator per run that records its kwargs and
# whether it was closed); the gates (``credits.guards``), the catalog check, the
# guest lookup and agent resolution are patched at their modules, so no test
# touches Mongo, the gateway or a CLI subprocess.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import InsufficientCredits
from pocketpaw_ee.cloud.auth import guest_budget
from pocketpaw_ee.cloud.byok.service import TurnCredentials
from pocketpaw_ee.cloud.chat import agent_service, side_completion
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.credits import guards
from pocketpaw_ee.cloud.studio import prompt_soften

from pocketpaw.agents import pool as agent_pool
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import get_settings

WS = "ws-side"
USER = "user-side"
DEFAULT_AGENT = "agent-default"
SYSTEM = "You sharpen website ideas."


class _ToolsBackend:
    async def run(
        self, message, *, system_prompt=None, tools_enabled=True, **_kw
    ):  # pragma: no cover
        yield None


class _NoToolsBackend:
    async def run(self, message, *, system_prompt=None):  # pragma: no cover
        yield None


def _msg(text: str) -> AgentEvent:
    return AgentEvent(type="message", content=text)


DONE = AgentEvent(type="done", content="")


class _Pool:
    def __init__(self, backend: str, model: str = "", *, tools: bool = True, delay: float = 0.0):
        self.instance = SimpleNamespace(
            config={"backend": backend, "model": model},
            backend=_ToolsBackend() if tools else _NoToolsBackend(),
        )
        self.events = [_msg("A brief "), _msg("for Crumb & Co."), DONE]
        self.delay = delay
        self.got: list[str] = []
        self.calls: list[dict] = []
        self.closed = False

    async def get(self, agent_id: str):
        self.got.append(agent_id)
        return self.instance

    def run(self, agent_id, message, session_key, **kwargs):
        self.calls.append(
            {"agent_id": agent_id, "message": message, "session_key": session_key, **kwargs}
        )
        return self._events()

    async def _events(self):
        try:
            for event in self.events:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield event
        finally:
            self.closed = True


@pytest.fixture
def seams(monkeypatch):
    """Every gate passes and the workspace default agent is ``agent-default``."""
    state = SimpleNamespace(
        creds=TurnCredentials(source="platform"),
        source="platform",
        visible={DEFAULT_AGENT, "agent-mine"},
        gateway_calls=[],
        catalog_checks=[],
    )

    async def _default(ws):
        assert ws == WS
        return DEFAULT_AGENT

    async def _visible(agent_id, ws, user):
        return ws == WS and user == USER and agent_id in state.visible

    async def _payer(ws):
        return state.creds

    async def _source(creds, agent_id, **_kw):
        return state.source

    async def _within(ws, source="platform", **_kw):
        return None

    async def _free_model(*_a):
        return None

    async def _bounds(*_a):
        return None

    async def _unknown(model_id):
        state.catalog_checks.append(model_id)
        return False

    async def _not_guest(_user):
        return None

    async def _gateway(*, model, system, user):
        state.gateway_calls.append({"model": model, "system": system, "user": user})
        return "fallback brief"

    async def _catalog_first():
        raise AssertionError("enhance must never pick the first catalog model")

    monkeypatch.setattr(agent_service, "_get_default_workspace_agent_id", _default)
    monkeypatch.setattr(side_completion, "_agent_visible", _visible)
    monkeypatch.setattr(guards, "resolve_payer", _payer)
    monkeypatch.setattr(guards, "billing_source", _source)
    monkeypatch.setattr(guards, "assert_within_billing", _within)
    monkeypatch.setattr(guards, "free_model_refusal", _free_model)
    monkeypatch.setattr(guards, "free_run_bounds", _bounds)
    monkeypatch.setattr(run_core, "_model_is_unknown_to_gateway", _unknown)
    monkeypatch.setattr(guest_budget, "load_guest", _not_guest)
    monkeypatch.setattr(prompt_soften, "_chat_completion", _gateway)
    monkeypatch.setattr(prompt_soften, "resolve_soften_model", _catalog_first)
    monkeypatch.setattr(get_settings(), "pydantic_ai_model", "litellm:claude-sonnet-4-6")
    return state


@pytest.fixture
def use_pool(monkeypatch):
    def _install(pool: _Pool) -> _Pool:
        monkeypatch.setattr(agent_pool, "get_agent_pool", lambda: pool)
        return pool

    return _install


async def _complete(**kw):
    args = {
        "workspace_id": WS,
        "user_id": USER,
        "system": SYSTEM,
        "prompt": "<original_prompt>a bakery</original_prompt>",
        "timeout_s": 5.0,
    }
    args.update(kw)
    return await side_completion.agent_complete(**args)


async def test_request_model_rides_to_the_agent_backend(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk", "claude-sonnet-4-6"))

    result = await _complete(model="claude-haiku-4-5")

    assert result.ok and result.text == "A brief for Crumb & Co."
    assert (result.model, result.backend, result.agent_id) == (
        "claude-haiku-4-5",
        "claude_agent_sdk",
        DEFAULT_AGENT,
    )
    (call,) = pool.calls
    assert call["model_override"] == "claude-haiku-4-5"
    assert seams.catalog_checks == ["claude-haiku-4-5"]


async def test_no_model_uses_the_agents_own_model(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk", "claude-sonnet-4-6"))

    result = await _complete()

    assert result.ok and result.model == "claude-sonnet-4-6"
    assert pool.got == [DEFAULT_AGENT]
    assert "model_override" not in pool.calls[0]


async def test_no_model_never_picks_the_first_catalog_entry(seams, use_pool):
    use_pool(_Pool("pydantic_ai", ""))

    result = await _complete()

    # resolve_soften_model raises if touched; the gateway helper is never called.
    assert result.ok
    assert seams.gateway_calls == [] and seams.catalog_checks == []


async def test_claude_sdk_runs_tool_less_with_no_request_cap(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk"))

    await _complete()

    (call,) = pool.calls
    assert call["tools_enabled"] is False
    assert "max_requests" not in call
    assert call["system_message_override"] == SYSTEM
    assert call["session_key"].startswith("side:")
    assert call["message"] == "<original_prompt>a bakery</original_prompt>"


async def test_each_call_gets_a_fresh_session(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk"))

    await _complete()
    await _complete()

    assert pool.calls[0]["session_key"] != pool.calls[1]["session_key"]


async def test_pydantic_ai_is_capped_at_one_request(seams, use_pool):
    pool = use_pool(_Pool("pydantic_ai", "litellm:gpt-5.6-luna"))

    result = await _complete()

    (call,) = pool.calls
    assert call["max_requests"] == 1 and call["tools_enabled"] is False
    assert result.backend == "pydantic_ai"


async def test_backend_without_a_tool_switch_falls_back_to_the_default_model(seams, use_pool):
    pool = use_pool(_Pool("codex_cli", tools=False))

    result = await _complete(model="gpt-5.6")

    assert pool.calls == []
    assert seams.gateway_calls[0]["model"] == "claude-sonnet-4-6"
    assert seams.gateway_calls[0]["system"] == SYSTEM
    assert result.ok and result.fallback and result.model == "claude-sonnet-4-6"


async def test_byok_backend_without_a_tool_switch_does_not_spend_our_gateway(seams, use_pool):
    seams.creds = TurnCredentials(source="byok", api_key="sk-x", provider="openai_compatible")
    seams.source = "byok"
    pool = use_pool(_Pool("codex_cli", "gpt-5.6", tools=False))

    result = await _complete()

    assert result.failure == "error" and pool.calls == [] and seams.gateway_calls == []


async def test_byok_provider_mismatch_fails(seams, use_pool):
    seams.creds = TurnCredentials(source="byok", api_key="sk-x", provider="openai")
    seams.source = "byok"
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete(model="claude-haiku-4-5")

    assert (result.failure, result.code) == ("model", "byok.model_provider_mismatch")
    assert pool.calls == []
    # A BYOK call is not judged against our gateway's catalog.
    assert seams.catalog_checks == []


async def test_byok_key_rides_to_the_pool(seams, use_pool):
    seams.creds = TurnCredentials(source="byok", api_key="sk-ant", provider="anthropic")
    seams.source = "byok"
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete(model="claude-haiku-4-5")

    assert result.ok and pool.calls[0]["byok_api_key"] == "sk-ant"


async def test_billing_refusal_is_typed(seams, use_pool, monkeypatch):
    async def _broke(*_a, **_kw):
        raise InsufficientCredits(1, 0)

    monkeypatch.setattr(guards, "assert_within_billing", _broke)
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete()

    assert (result.failure, result.code) == ("billing", "credits.insufficient")
    assert pool.got == [] and pool.calls == []


async def test_free_model_refusal_is_billing(seams, use_pool, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import ModelNotAllowed

    async def _refuse(*_a):
        return ModelNotAllowed("claude-opus-4-1")

    monkeypatch.setattr(guards, "free_model_refusal", _refuse)
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete(model="claude-opus-4-1")

    assert result.failure == "billing" and pool.calls == []


async def test_keyless_guest_is_refused(seams, use_pool, monkeypatch):
    async def _guest(_user):
        return object()

    monkeypatch.setattr(guest_budget, "load_guest", _guest)
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete()

    assert (result.failure, result.code) == ("billing", "guest_key_required")
    assert pool.calls == []


async def test_unknown_platform_model_is_refused(seams, use_pool, monkeypatch):
    async def _unknown(_model):
        return True

    monkeypatch.setattr(run_core, "_model_is_unknown_to_gateway", _unknown)
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete(model="free-text-model")

    assert (result.failure, result.code) == ("model", "model.not_available")
    assert pool.calls == []


async def test_free_run_is_forced_onto_pydantic_ai_and_bounded(seams, use_pool, monkeypatch):
    async def _bounds(ws, source):
        return guards.FreeRunBounds(max_turns=5, balance_micro=10_000)

    monkeypatch.setattr(guards, "free_run_bounds", _bounds)
    monkeypatch.setattr(guards, "free_run_token_limit", lambda balance, model: 1234)
    pool = use_pool(_Pool("claude_agent_sdk", "claude-sonnet-4-6"))

    result = await _complete()

    (call,) = pool.calls
    assert call["force_pydantic_ai"] is True
    assert call["max_requests"] == 1
    assert call["total_tokens_limit"] == 1234
    assert "byok_api_key" not in call
    assert result.ok and result.backend == "pydantic_ai"


async def test_agent_from_another_workspace_is_rejected(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete(agent_id="agent-elsewhere")

    assert result.failure == "no_agent"
    assert pool.got == [] and pool.calls == []


async def test_visible_agent_hint_is_used(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk"))

    result = await _complete(agent_id="agent-mine")

    assert result.ok and pool.got == ["agent-mine"]


async def test_timeout_fails_and_closes_the_generator(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk", delay=1.0))

    result = await _complete(timeout_s=0.05)

    assert result.failure == "timeout"
    assert pool.closed


async def test_timeout_is_sized_by_the_serving_backend(seams, use_pool):
    use_pool(_Pool("claude_agent_sdk"))
    asked: list[str] = []

    def _budget(backend: str) -> float:
        asked.append(backend)
        return 5.0

    await _complete(timeout_s=_budget)

    assert asked == ["claude_agent_sdk"]


async def test_generator_is_closed_after_done(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk"))
    pool.events = [_msg("first"), DONE, _msg("never read")]

    result = await _complete()

    assert result.text == "first"
    assert pool.closed


async def test_backend_error_event_fails(seams, use_pool):
    pool = use_pool(_Pool("claude_agent_sdk"))
    pool.events = [AgentEvent(type="error", content="provider exploded")]

    result = await _complete()

    assert (result.failure, result.code) == ("error", "agent.backend_error")
    assert pool.closed


async def test_never_raises(seams, use_pool, monkeypatch):
    async def _boom(_ws):
        raise RuntimeError("db down")

    monkeypatch.setattr(guards, "resolve_payer", _boom)
    use_pool(_Pool("claude_agent_sdk"))

    result = await _complete()

    assert result.failure == "error"
