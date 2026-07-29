# tests/cloud/runs/test_run_core_agent_tool_policy.py
# Created 2026-07-24 (CX-2, feat/code-agent-exclusive-tools) — pins the
# per-agent EXCLUSIVE tool policy resolution + its run_kwargs wiring in run_core:
#   * _agent_tool_policy: (True, frozenset(tools)) only for tool_mode="exclusive";
#     (False, frozenset()) for "additive" and for a config with no tool_mode;
#     tolerant of a dict OR an object config.
#   * _drive_agent_loop: an exclusive agent sets run_kwargs["exclusive_mcp_tools"]
#     = True and OVERRIDES allow_mcp_tool_ids with the agent's own tools, even
#     when the surface set a different allow_mcp. An additive agent touches
#     neither key and leaves any surface allow_mcp intact (grant path unchanged).
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

# ---------------------------------------------------------------------------
# _agent_tool_policy (pure unit)
# ---------------------------------------------------------------------------


def _instance(config: Any) -> Any:
    return SimpleNamespace(config=config)


def test_tool_policy_exclusive_returns_tools():
    inst = _instance({"tool_mode": "exclusive", "tools": ["mcp__code__read", "mcp__code__write"]})
    assert run_core._agent_tool_policy(inst) == (
        True,
        frozenset({"mcp__code__read", "mcp__code__write"}),
    )


def test_tool_policy_additive_returns_false_empty():
    inst = _instance({"tool_mode": "additive", "tools": ["mcp__code__read"]})
    assert run_core._agent_tool_policy(inst) == (False, frozenset())


def test_tool_policy_no_tool_mode_defaults_additive():
    # A non-empty tools list alone does NOT make an agent exclusive.
    inst = _instance({"tools": ["mcp__code__read"]})
    assert run_core._agent_tool_policy(inst) == (False, frozenset())


def test_tool_policy_empty_config():
    assert run_core._agent_tool_policy(_instance({})) == (False, frozenset())


def test_tool_policy_object_config():
    """The resolver tolerates an object config (getattr path) too."""
    cfg = SimpleNamespace(tool_mode="exclusive", tools=["mcp__code__ls"])
    assert run_core._agent_tool_policy(_instance(cfg)) == (True, frozenset({"mcp__code__ls"}))


# ---------------------------------------------------------------------------
# _drive_agent_loop → run_kwargs wiring
# ---------------------------------------------------------------------------


def _scope_ctx(*, resolved_profile=None) -> ScopeContext:
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    ctx.resolved_profile = resolved_profile
    return ctx


async def _drain_run_kwargs(monkeypatch, ctx, agent_config: dict[str, Any]) -> dict[str, Any]:
    """Drive _drive_agent_loop just far enough to capture the run_kwargs the
    pool.run seam receives, then stop. Mocks every collaborator the loop touches
    before pool.run."""
    captured: dict[str, Any] = {}

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config=agent_config, agent_name="A")

        def run(self, agent_id, content, session_key, **run_kwargs):
            captured["run_kwargs"] = run_kwargs

            async def _gen():
                return
                yield  # pragma: no cover

            return _gen()

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)

    async def _never_cancelled():
        return False

    gen = run_core._drive_agent_loop(
        ctx,
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=None,
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    )
    async for _ in gen:
        pass
    return captured


@pytest.mark.asyncio
async def test_exclusive_agent_sets_flag_and_allow(monkeypatch):
    """An exclusive agent (resolved_profile None) sets exclusive_mcp_tools=True
    and allow_mcp_tool_ids = its own declared tools."""
    ctx = _scope_ctx(resolved_profile=None)
    captured = await _drain_run_kwargs(
        monkeypatch,
        ctx,
        {"tool_mode": "exclusive", "tools": ["mcp__code__read", "mcp__code__write"]},
    )
    rk = captured["run_kwargs"]
    assert rk["exclusive_mcp_tools"] is True
    assert rk["allow_mcp_tool_ids"] == frozenset({"mcp__code__read", "mcp__code__write"})


@pytest.mark.asyncio
async def test_exclusive_agent_overrides_surface_allow(monkeypatch):
    """When the surface already set allow_mcp_tool_ids, an exclusive agent WINS —
    its own tools replace the surface allow-list."""
    profile = SimpleNamespace(
        deny_mcp_tool_ids=frozenset(),
        allowed_sdk_tools=frozenset(),
        allow_mcp_tool_ids=frozenset({"mcp__surface__tool"}),
        system_message_override=None,
        skill_names=frozenset(),
    )
    ctx = _scope_ctx(resolved_profile=profile)
    captured = await _drain_run_kwargs(
        monkeypatch,
        ctx,
        {"tool_mode": "exclusive", "tools": ["mcp__code__read"]},
    )
    rk = captured["run_kwargs"]
    assert rk["exclusive_mcp_tools"] is True
    assert rk["allow_mcp_tool_ids"] == frozenset({"mcp__code__read"})


@pytest.mark.asyncio
async def test_additive_agent_touches_nothing(monkeypatch):
    """An additive agent adds no exclusive key and leaves a surface allow_mcp
    intact — the grant path is byte-identical to today."""
    profile = SimpleNamespace(
        deny_mcp_tool_ids=frozenset(),
        allowed_sdk_tools=frozenset(),
        allow_mcp_tool_ids=frozenset({"mcp__surface__tool"}),
        system_message_override=None,
        skill_names=frozenset(),
    )
    ctx = _scope_ctx(resolved_profile=profile)
    captured = await _drain_run_kwargs(
        monkeypatch,
        ctx,
        {"tool_mode": "additive", "tools": ["mcp__code__read"]},
    )
    rk = captured["run_kwargs"]
    assert "exclusive_mcp_tools" not in rk
    assert rk["allow_mcp_tool_ids"] == frozenset({"mcp__surface__tool"})


@pytest.mark.asyncio
async def test_no_tool_mode_is_additive(monkeypatch):
    """A legacy config with no tool_mode and no surface allow_mcp: neither key
    is set (broad surfaces keep every tool)."""
    ctx = _scope_ctx(resolved_profile=None)
    captured = await _drain_run_kwargs(monkeypatch, ctx, {"tools": ["mcp__code__read"]})
    rk = captured["run_kwargs"]
    assert "exclusive_mcp_tools" not in rk
    assert "allow_mcp_tool_ids" not in rk
