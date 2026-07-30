# tests/cloud/runs/test_run_core_connector_skill_surfacing.py
# Created: 2026-07-16 (feat/senses-skill-surfacing, SR-5) — end-to-end pins that a
#   connector's DERIVED skill (M3 ``derive_surface_profile``) surfaces in the NEXT
#   agent run's materialized skill set for the bound pocket. Ties the two halves
#   that were previously only tested in isolation — connector-bind →
#   ``pocket.surface_profile`` (``test_surface_profile_authoring``) and
#   ``surface_profile.skill_names`` → run forward
#   (``test_run_core_entity_profile_threading``) — into ONE "connect once, the
#   agent immediately knows how" narrative through the REAL derive → model_dump →
#   ``_resolve_entity_profile`` (compose) → ``_drive_agent_loop`` forward path
#   (only the agent pool and the pocket-load are stubbed). Asserts the four SR-5
#   contracts: binding SURFACES the skill; the derived skill UNIONs with (never
#   replaces) the agent's own ``skill_refs``; unbinding removes ONLY the derived
#   skill; a skill-only connector leaves ``ripple_mode`` / ``deny_mcp_tool_ids``
#   untouched. No pocketpaw_ee symbol crosses the EE→OSS boundary — only a
#   ``frozenset[str]`` reaches ``AgentPool.run(skill_names=...)``.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.connectors.derivation import derive_surface_profile
from pocketpaw_ee.cloud.connectors.domain import (
    AvailableConnector,
    ConnectorSurfaceContribution,
)
from pocketpaw_ee.cloud.surface import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    resolve_profile,
)

pytestmark = pytest.mark.asyncio


def _connector(name: str, skill: str) -> AvailableConnector:
    """A minimal available connector whose surface contribution is one skill."""
    return AvailableConnector(
        name=name,
        display_name=name.title(),
        type="communication",
        icon="",
        auth_method="oauth",
        surface_profile=ConnectorSurfaceContribution(skill=skill),
    )


class _CapturingPool:
    """Pool stub: captures ``run`` kwargs and lets a test seed the agent instance
    config so ``_agent_skill_set`` can see the agent's own ``skill_refs``."""

    def __init__(self, instance_config: dict[str, Any] | None = None) -> None:
        self.run_kwargs: dict[str, Any] | None = None
        self._config = {"backend": "claude_agent_sdk", **(instance_config or {})}

    async def get(self, _agent_id):
        config = self._config
        return type("Inst", (), {"config": config})()

    def run(self, *args, **kwargs):
        self.run_kwargs = kwargs

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


def _ctx() -> ScopeContext:
    return ScopeContext(
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


async def _resolve_with_connectors(monkeypatch, ctx, connectors) -> None:
    """Exercise the REAL derive → model_dump → ``_load_entity_profile_override`` →
    ``compose_entity_profile`` path and stash the result on ``ctx.resolved_profile``
    exactly as ``execute_run`` does once per run. ``connectors`` is the pocket's
    enabled set; the empty set models a fully-unbound pocket (derive → ``None``)."""
    derived = derive_surface_profile(connectors)
    override = derived.model_dump() if derived is not None else None

    async def _fake_load(_ws, _pkt):
        return override

    monkeypatch.setattr(run_core, "_load_entity_profile_override", _fake_load)
    ctx.resolved_profile = await run_core._resolve_entity_profile(ctx)


async def _drive(monkeypatch, ctx, pool) -> _CapturingPool:
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


async def test_bound_connector_skill_surfaces_in_next_run(monkeypatch):
    """Binding gmail (skill 'gmail') to a pocket surfaces that skill in the run."""
    ctx = _ctx()
    await _resolve_with_connectors(monkeypatch, ctx, [_connector("gmail", "gmail")])
    pool = await _drive(monkeypatch, ctx, _CapturingPool())
    assert pool.run_kwargs is not None
    assert pool.run_kwargs.get("skill_names") == frozenset({"gmail"})


async def test_derived_skill_unions_with_agent_skill_refs(monkeypatch):
    """The derived connector skill UNIONs with the agent's own skill_refs — the
    derived skill is ADDED, the pre-existing skill_refs are NOT replaced."""
    ctx = _ctx()
    await _resolve_with_connectors(monkeypatch, ctx, [_connector("github", "github")])
    pool = await _drive(
        monkeypatch, ctx, _CapturingPool(instance_config={"skill_refs": ["team-playbook"]})
    )
    assert pool.run_kwargs.get("skill_names") == frozenset({"github", "team-playbook"})


async def test_unbind_removes_only_derived_skill(monkeypatch):
    """A fully-unbound pocket (no connectors) drops the derived skill while the
    agent's pre-existing skill_refs stay intact — union removal, not replacement."""
    ctx = _ctx()
    await _resolve_with_connectors(monkeypatch, ctx, [])  # re-derive from empty set
    pool = await _drive(
        monkeypatch, ctx, _CapturingPool(instance_config={"skill_refs": ["team-playbook"]})
    )
    assert pool.run_kwargs.get("skill_names") == frozenset({"team-playbook"})


async def test_skill_only_connector_leaves_ripple_and_deny_unchanged(monkeypatch):
    """A skill-only connector must not perturb ripple_mode / deny_mcp_tool_ids —
    those dims stay whatever the surface base composed to."""
    base = resolve_profile(SurfaceKind.CHAT, SurfaceMeta())
    ctx = _ctx()
    await _resolve_with_connectors(monkeypatch, ctx, [_connector("gmail", "gmail")])
    assert ctx.resolved_profile.ripple_mode == base.ripple_mode
    assert ctx.resolved_profile.deny_mcp_tool_ids == base.deny_mcp_tool_ids
    pool = await _drive(monkeypatch, ctx, _CapturingPool())
    assert pool.run_kwargs.get("deny_mcp_tool_ids") == base.deny_mcp_tool_ids
