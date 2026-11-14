# tests/cloud/runs/test_run_core_sites_soul_scope.py
# Pins UX-8: content must not bleed between unrelated Sites chats.
#
# The channel was the per-agent soul. Every completed turn fed
# ``pool.observe(agent, user_message, reply)`` into the agent's soul, and every
# new turn's prompt got a ``## Relevant Past Memories`` block recalled against
# the user's message. Two Sites briefs ("a site for my pottery studio", "a site
# for my tattoo studio") are near-identical queries, so the tattoo chat was
# handed the pottery chat's closing suggestions and the architect site's
# "no gradients" rules. A Sites chat is about one site: its turns now neither
# train the soul nor read its cross-session recall. Other surfaces keep both.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.surface import SurfaceContext, SurfaceKind, SurfaceMeta

from pocketpaw.agents.pool import AgentPool

pytestmark = pytest.mark.asyncio

_POTTERY = "Want me to swap in a darker clay tone, add a shop section, or a bento gallery?"


def _surface(kind: SurfaceKind) -> SurfaceContext:
    return SurfaceContext(
        workspace_id="w1",
        user_id="u1",
        kind=kind,
        meta=SurfaceMeta(),
        preamble="",
        preamble_cache_key=None,
    )


def _ctx(kind: SurfaceKind | None) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        surface_context=_surface(kind) if kind else None,
    )


def _spec() -> RunSpec:
    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="cloud:session:s1:a1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="build a site for my pottery studio",
        history=[],
        intent=None,
    )


# --- the write half: a Sites turn does not train the soul ---------------------


def _install_persist_stubs(monkeypatch, observe: AsyncMock) -> None:
    async def _fake_persist(ctx, content, attachments, **_):
        return SimpleNamespace(id="assistant-1", createdAt=datetime.now(UTC))

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(run_core, "_persist_assistant_message", _fake_persist)
    monkeypatch.setattr(run_core.run_service, "mark_completed", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: SimpleNamespace(observe=observe))


async def test_a_sites_turn_does_not_feed_the_agent_soul(monkeypatch):
    observe = AsyncMock()
    _install_persist_stubs(monkeypatch, observe)

    await run_core._persist_and_complete(_spec(), _ctx(SurfaceKind.SITES), _POTTERY, [])

    observe.assert_not_called()


async def test_a_pocket_surface_turn_still_feeds_the_agent_soul(monkeypatch):
    observe = AsyncMock()
    _install_persist_stubs(monkeypatch, observe)

    await run_core._persist_and_complete(_spec(), _ctx(SurfaceKind.POCKET), _POTTERY, [])

    observe.assert_awaited_once()


# --- the read half: a Sites turn gets no cross-session recall -----------------


class _CapturingPool:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    def run(self, *args, **kwargs):
        self.run_kwargs = kwargs

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()

    async def prewarm(self, *args, **kwargs):
        return None


async def _drive(monkeypatch, ctx) -> _CapturingPool:
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(*a, **k):
        return "KB"

    async def _is_cancelled():
        return False

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    async for _ in run_core._drive_agent_loop(
        ctx,
        user_content="build a site for my tattoo studio",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_is_cancelled,
        emit_stream_start=False,
    ):
        pass
    return pool


async def test_a_sites_turn_asks_the_pool_for_no_soul_recall(monkeypatch):
    pool = await _drive(monkeypatch, _ctx(SurfaceKind.SITES))
    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("soul_recall") is False


async def test_other_turns_keep_the_default_recall(monkeypatch):
    pool = await _drive(monkeypatch, _ctx(SurfaceKind.POCKET))
    assert pool.run_kwargs is not None
    assert "soul_recall" not in pool.run_kwargs


class _PotterySoul:
    async def context_for(self, message: str, **kwargs) -> str:  # noqa: ARG002
        return f"- {_POTTERY}"


def _instance():
    return SimpleNamespace(
        backend=None,
        soul_manager=SimpleNamespace(bootstrap_provider=None, soul=_PotterySoul()),
        config={"soul_persona": "WHO I AM", "system_prompt": ""},
        created_from_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _assemble(**kwargs):
    return await AgentPool()._assemble_system_prompt(
        _instance(),
        agent_id="a1",
        message="build a site for my tattoo studio",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        **kwargs,
    )


async def test_the_prompt_carries_no_past_memories_when_recall_is_off():
    with_recall = await _assemble()
    without = await _assemble(soul_recall=False)

    assert _POTTERY in with_recall.text, "the fixture is not exercising the recall"
    assert _POTTERY not in without.text
    assert "Relevant Past Memories" not in without.text
    # Recall is a volatile layer, so turning it off must not move the digest.
    assert with_recall.stable_digest == without.stable_digest


# --- the tool half: a Sites turn cannot call the soul memory tools ------------


async def test_a_sites_run_denies_the_soul_memory_tools(monkeypatch):
    from pocketpaw_ee.cloud.surface.service import resolve_profile

    ctx = _ctx(SurfaceKind.SITES)
    base = resolve_profile(SurfaceKind.SITES, SurfaceMeta())
    ctx.resolved_profile = run_core._deny_soul_tools_on_sites(ctx, base)

    assert {"soul_recall", "soul_remember"} <= ctx.resolved_profile.deny_mcp_tool_ids
    assert base.deny_mcp_tool_ids <= ctx.resolved_profile.deny_mcp_tool_ids

    pool = await _drive(monkeypatch, ctx)
    assert pool.run_kwargs is not None
    assert "soul_recall" in pool.run_kwargs["deny_mcp_tool_ids"]


async def test_other_surfaces_keep_the_soul_memory_tools():
    from pocketpaw_ee.cloud.surface.service import resolve_profile

    ctx = _ctx(SurfaceKind.POCKET)
    base = resolve_profile(SurfaceKind.POCKET, SurfaceMeta())
    assert run_core._deny_soul_tools_on_sites(ctx, base) is base
