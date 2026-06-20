# tests/cloud/runs/test_run_core_agent_skills.py
# Created 2026-06-08 (feat/agent-plugin-fields, M2) — pins the per-agent skill
# fold into the per-run skill materialization in run_core:
#   * _agent_skill_set: skill_refs only; plugins resolved via a monkeypatched
#     PluginInstaller.list_plugins → union; unknown plugin name ignored;
#     resolution failure → empty (no raise).
#   * _drive_agent_loop: the agent's skills land in run_kwargs["skill_names"]
#     alongside surface skills (resolved_profile set) AND on the legacy
#     resolved_profile-None path. Withhold-when-empty stays intact.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

# ---------------------------------------------------------------------------
# _agent_skill_set
# ---------------------------------------------------------------------------


def _instance(config: dict[str, Any]) -> Any:
    return SimpleNamespace(config=config)


def test_agent_skill_set_skill_refs_only():
    inst = _instance({"skill_refs": ["github", "jira"]})
    assert run_core._agent_skill_set(inst) == frozenset({"github", "jira"})


def test_agent_skill_set_no_fields_empty():
    assert run_core._agent_skill_set(_instance({})) == frozenset()


def test_agent_skill_set_resolves_plugins(monkeypatch):
    """Enabled plugins resolve to their bundled skills via the registry; the
    union with direct skill_refs is returned, unknown plugin names ignored."""

    fake_plugins = [
        SimpleNamespace(name="acme", skills=["alpha", "beta"]),
        SimpleNamespace(name="other", skills=["gamma"]),
    ]
    monkeypatch.setattr(
        "pocketpaw.plugins.installer.PluginInstaller.list_plugins",
        lambda self: fake_plugins,
    )

    inst = _instance({"skill_refs": ["direct"], "plugins": ["acme", "unknown"]})
    out = run_core._agent_skill_set(inst)
    # direct ref + acme's two skills; "unknown" silently ignored; "other" not enabled.
    assert out == frozenset({"direct", "alpha", "beta"})


def test_agent_skill_set_missing_registry_returns_empty(monkeypatch):
    """A failing registry read must never raise — plugin skills degrade to
    empty, but the direct skill_refs still come through."""

    def _boom(self):
        raise RuntimeError("no registry on disk")

    monkeypatch.setattr("pocketpaw.plugins.installer.PluginInstaller.list_plugins", _boom)

    inst = _instance({"skill_refs": ["safe"], "plugins": ["acme"]})
    assert run_core._agent_skill_set(inst) == frozenset({"safe"})


# ---------------------------------------------------------------------------
# _drive_agent_loop union into run_kwargs["skill_names"]
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
    pool.run seam receives, then stop. Mocks every collaborator the loop
    touches before pool.run."""
    captured: dict[str, Any] = {}

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config=agent_config, agent_name="A")

        def run(self, agent_id, content, session_key, **run_kwargs):
            captured["run_kwargs"] = run_kwargs

            async def _gen():
                # Empty async generator: the first __anext__ raises
                # StopAsyncIteration, which the loop treats as "stream done"
                # and breaks. (A bare `raise StopAsyncIteration` inside an
                # async gen would become a RuntimeError under PEP 479.)
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
    # Pull events until the loop terminates (StopAsyncIteration from the gen).
    async for _ in gen:
        pass
    return captured


@pytest.mark.asyncio
async def test_agent_skills_fold_in_with_resolved_profile(monkeypatch):
    """surface skills (from resolved_profile) UNION agent skills → both land in
    run_kwargs["skill_names"]."""

    profile = SimpleNamespace(
        deny_mcp_tool_ids=frozenset(),
        allowed_sdk_tools=frozenset(),
        allow_mcp_tool_ids=None,
        system_message_override=None,
        skill_names=frozenset({"surface-skill"}),
    )
    ctx = _scope_ctx(resolved_profile=profile)
    captured = await _drain_run_kwargs(monkeypatch, ctx, {"skill_refs": ["agent-skill"]})
    assert captured["run_kwargs"]["skill_names"] == frozenset({"surface-skill", "agent-skill"})


@pytest.mark.asyncio
async def test_agent_skills_fold_in_on_resolved_profile_none(monkeypatch):
    """Legacy path (resolved_profile is None): agent skills STILL materialize —
    the fold happens regardless of the profile guard."""
    ctx = _scope_ctx(resolved_profile=None)
    captured = await _drain_run_kwargs(monkeypatch, ctx, {"skill_refs": ["agent-skill"]})
    assert captured["run_kwargs"]["skill_names"] == frozenset({"agent-skill"})


@pytest.mark.asyncio
async def test_no_skills_withholds_skill_names(monkeypatch):
    """No surface skills and no agent skills → skill_names is NOT set (the
    withhold-when-empty contract for the narrower non-SDK backends)."""
    ctx = _scope_ctx(resolved_profile=None)
    captured = await _drain_run_kwargs(monkeypatch, ctx, {})
    assert "skill_names" not in captured["run_kwargs"]
