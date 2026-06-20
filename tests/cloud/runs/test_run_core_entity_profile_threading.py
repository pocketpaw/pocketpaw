# tests/cloud/runs/test_run_core_entity_profile_threading.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A1/A2) —
# pins that ``_drive_agent_loop`` READS the two formerly-inert SurfaceProfile
# fields off ``ctx.resolved_profile`` and forwards them to ``AgentPool.run`` as
# plain data: ``system_message_override`` (forwarded when not None) and
# ``skill_names`` (forwarded when non-empty). No pocketpaw_ee symbol crosses the
# boundary — only a str and a frozenset.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.surface import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    compose_entity_profile,
    resolve_profile,
)

pytestmark = pytest.mark.asyncio


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


def _ctx_with_profile(profile) -> ScopeContext:
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        surface_context=SurfaceContext(
            workspace_id="w1",
            user_id="u1",
            kind=SurfaceKind.CHAT,
            meta=SurfaceMeta(pocket_id="pkt_1"),
            preamble="",
        ),
    )
    ctx.resolved_profile = profile
    return ctx


async def _drive(monkeypatch, ctx) -> _CapturingPool:
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None, raising=False)

    async def _is_cancelled():
        return False

    gen = run_core._drive_agent_loop(
        ctx,
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=_is_cancelled,
        emit_stream_start=False,
    )
    async for _ in gen:
        pass
    return pool


async def test_threads_override_and_skills(monkeypatch):
    base = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())
    entity = compose_entity_profile(
        base,
        {"system_message_override": "ENTITY SYS", "skill_names": ["github"]},
    )
    pool = await _drive(monkeypatch, _ctx_with_profile(entity))
    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("system_message_override") == "ENTITY SYS"
    assert pool.run_kwargs.get("skill_names") == frozenset({"github"})


async def test_withholds_when_unset(monkeypatch):
    base = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())  # no override, no skills
    pool = await _drive(monkeypatch, _ctx_with_profile(base))
    assert pool.run_kwargs is not None
    assert "system_message_override" not in pool.run_kwargs
    assert "skill_names" not in pool.run_kwargs
