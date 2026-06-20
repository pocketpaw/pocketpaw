# tests/cloud/runs/test_run_core_entity_profile.py
# Created: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①)
# — pins the ENTITY-AWARE once-per-run SurfaceProfile resolution in run_core:
#   * ``_resolve_entity_profile`` composes the entity pocket's surface_profile
#     override OVER the surface base (ripple entity-wins, deny UNION, allow UNION).
#   * ``meta.pocket_id`` unset OR pocket has no override → resolved == surface
#     base (zero regression / legacy path).
#   * cloud Rule 7: a pocket outside the run's workspace is IGNORED (no
#     cross-tenant profile read).
#   * ``execute_run`` stashes the resolved profile on ``ctx.resolved_profile``
#     and BOTH consumers read it: build_behavior_instructions (ripple) +
#     _drive_agent_loop tool-deny / tool-allow.

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    build_behavior_instructions,
)
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.surface import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
)

from pocketpaw.ripple import INLINE_RIPPLE_SYSTEM_PROMPT

pytestmark = pytest.mark.asyncio


def _entity_surface_ctx(meta: SurfaceMeta) -> ScopeContext:
    """A ScopeContext whose resolved surface carries ``meta`` (e.g. a pocket_id
    pinning it to an entity room)."""
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        surface_context=SurfaceContext(
            workspace_id="w1",
            user_id="u1",
            kind=SurfaceKind.CHAT,
            meta=meta,
            preamble="",
        ),
    )


# ---------------------------------------------------------------------------
# _resolve_entity_profile — composition + tenant scoping
# ---------------------------------------------------------------------------


async def test_resolve_entity_profile_composes_override_over_base(monkeypatch):
    """An entity pocket with a surface_profile override composes OVER the
    surface base: ripple entity-wins, deny UNIONs."""

    async def _fake_load(workspace_id, pocket_id):
        # The JSON-shaped override the pocket carries.
        return {
            "ripple_mode": "off",
            "deny_mcp_tool_ids": ["refund"],
            "skill_names": ["github"],
            "allowed_sdk_tools": ["WebFetch"],
        }

    monkeypatch.setattr(run_core, "_load_entity_profile_override", _fake_load)

    ctx = _entity_surface_ctx(SurfaceMeta(pocket_id="pkt_1"))
    profile = await run_core._resolve_entity_profile(ctx)

    # CHAT base is ripple-on with no deny; the entity flips ripple off and adds denies.
    assert profile.ripple_mode == "off"
    assert "refund" in profile.deny_mcp_tool_ids
    assert profile.skill_names == frozenset({"github"})
    assert profile.allowed_sdk_tools == frozenset({"WebFetch"})


async def test_resolve_entity_profile_no_pocket_is_base(monkeypatch):
    """No ``pocket_id`` on the meta → resolved profile is the pure surface base
    (zero regression). The loader is never even consulted."""
    called = {"load": False}

    async def _fake_load(workspace_id, pocket_id):
        called["load"] = True
        return {"ripple_mode": "off"}

    monkeypatch.setattr(run_core, "_load_entity_profile_override", _fake_load)

    ctx = _entity_surface_ctx(SurfaceMeta())  # no pocket_id
    profile = await run_core._resolve_entity_profile(ctx)

    assert profile.ripple_mode == "on"  # CHAT base
    assert profile.deny_mcp_tool_ids == frozenset()
    assert called["load"] is False, "no pocket_id must not trigger a pocket load"


async def test_resolve_entity_profile_pocket_without_override_is_base(monkeypatch):
    """A pocket that exists but carries NO surface_profile (loader returns None)
    → resolved profile equals the base unchanged."""

    async def _fake_load(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(run_core, "_load_entity_profile_override", _fake_load)

    ctx = _entity_surface_ctx(SurfaceMeta(pocket_id="pkt_1"))
    profile = await run_core._resolve_entity_profile(ctx)
    assert profile.ripple_mode == "on"
    assert profile.deny_mcp_tool_ids == frozenset()


async def test_resolve_entity_profile_surface_context_none_is_default():
    """``surface_context is None`` (legacy clients) → the safe default profile
    (ripple on, no deny) — today's behavior exactly."""
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    profile = await run_core._resolve_entity_profile(ctx)
    assert profile.ripple_mode == "on"
    assert profile.deny_mcp_tool_ids == frozenset()


# ---------------------------------------------------------------------------
# _load_entity_profile_override — tenant scoping (cloud Rule 7)
# ---------------------------------------------------------------------------


async def test_load_override_ignores_cross_tenant_pocket(monkeypatch):
    """Rule 7: a pocket whose ``workspace`` differs from the run's workspace is
    IGNORED — never read another tenant's profile."""

    class _ForeignPocket:
        workspace = "OTHER_WS"
        surface_profile = type("P", (), {"model_dump": lambda self: {"ripple_mode": "off"}})()

    class _PocketModel:
        @staticmethod
        async def get(_oid):
            return _ForeignPocket()

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.models.pocket.Pocket",
        _PocketModel,
        raising=False,
    )
    # PydanticObjectId(pocket_id) must not raise for a 24-hex id.
    out = await run_core._load_entity_profile_override("w1", "a" * 24)
    assert out is None, "cross-tenant pocket must be ignored"


async def test_load_override_returns_dump_for_same_tenant(monkeypatch):
    """A same-workspace pocket with a surface_profile returns its model_dump dict."""

    class _Override:
        def model_dump(self):
            return {"ripple_mode": "trim", "deny_mcp_tool_ids": ["x"]}

    class _Pocket:
        workspace = "w1"
        surface_profile = _Override()

    class _PocketModel:
        @staticmethod
        async def get(_oid):
            return _Pocket()

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.models.pocket.Pocket",
        _PocketModel,
        raising=False,
    )
    out = await run_core._load_entity_profile_override("w1", "a" * 24)
    assert out == {"ripple_mode": "trim", "deny_mcp_tool_ids": ["x"]}


# ---------------------------------------------------------------------------
# Both consumers read ctx.resolved_profile
# ---------------------------------------------------------------------------


async def test_build_behavior_instructions_reads_resolved_profile():
    """``build_behavior_instructions`` gates the ripple block on the PRE-RESOLVED
    ``ctx.resolved_profile`` — a pocket-entity that flipped ripple off in its
    room OMITS the ripple LAW, with no /sites surface involved."""
    from pocketpaw_ee.cloud.surface import compose_entity_profile, resolve_profile

    base = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())  # ripple on
    entity = compose_entity_profile(base, {"ripple_mode": "off"})

    ctx = _entity_surface_ctx(SurfaceMeta(pocket_id="pkt_1"))
    ctx.resolved_profile = entity
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert INLINE_RIPPLE_SYSTEM_PROMPT not in block, (
        "an entity room with ripple_mode='off' must omit the ripple LAW"
    )


# ---------------------------------------------------------------------------
# execute_run stashes the resolved profile and threads deny + allow
# ---------------------------------------------------------------------------


def _entity_spec() -> RunSpec:
    """A run bound to a pocket-entity: surface=chat + meta.pocket_id."""
    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="work with my orders",
        history=[],
        intent=None,
        surface="chat",
        surface_meta={"pocket_id": "pkt_1"},
    )


def _scope_only_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _noop(*a, **k):
    return None


async def _persist_stub(spec, ctx, full_text, attachments, usage=None):
    return "assistant-msg-1"


async def test_execute_run_stashes_entity_resolved_profile(monkeypatch):
    """End-to-end: execute_run resolves the entity profile once and stashes it
    on ctx.resolved_profile — the deny UNION + ripple override reach the loop."""
    captured: dict[str, Any] = {}

    async def _capture_ctx(spec, ctx):
        captured["resolved_profile"] = ctx.resolved_profile
        return
        yield  # pragma: no cover

    async def _fake_resolve_scope_context(**_):
        return _scope_only_ctx()

    async def _fake_load(workspace_id, pocket_id):
        return {"ripple_mode": "off", "deny_mcp_tool_ids": ["refund"]}

    monkeypatch.setattr(run_core, "_iter_agent_events", _capture_ctx)
    monkeypatch.setattr(run_core, "resolve_scope_context", _fake_resolve_scope_context)
    monkeypatch.setattr(run_core, "_load_entity_profile_override", _fake_load)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _noop)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)

    await run_core.execute_run(_entity_spec())

    rp = captured.get("resolved_profile")
    assert rp is not None, "execute_run must stash ctx.resolved_profile before the loop"
    assert rp.ripple_mode == "off"  # entity override won
    assert "refund" in rp.deny_mcp_tool_ids  # entity deny present
