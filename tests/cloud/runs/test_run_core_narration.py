# tests/cloud/runs/test_run_core_narration.py
# Created 2026-08-15 (HTN-11) — pins tool narration on the STREAMING chat path.
#
# HTN-1 wired narration to the group/DM bridge only, so this surface — the one
# most users actually watch — kept emitting bare tool names while group/DM read
# as English. Nothing failed. Each surface had its own tests, both passed, and
# neither compared against the other.
#
# So the load-bearing test here is not "run_core emits a narration". It is
# ``test_both_surfaces_narrate_the_same_call_identically``, which drives ONE
# backend event through BOTH paths and asserts the phrases match. A test that
# only checked this file would have gone green on the day the bug shipped.
#
# The backend events use the shape pydantic-ai actually emits: ``content`` is
# the prose "Using web_search...", ``metadata`` carries the bare name plus the
# call's argument dict (``agents/pydantic_ai.py::_announce_tool``).
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio


def _tool_use_event(name: str, tool_input: dict):
    return SimpleNamespace(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": tool_input},
    )


def _done():
    return SimpleNamespace(type="done", content="")


def _scope_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


def _bridged_backend():
    """A stand-in for a backend that bridged its tools — pydantic_ai, and the
    three others that go through ``_build_tool_registry``.

    Built through the PUBLIC builder rather than by hand, because the thing
    under test is whether ``narration_registry_for`` can reach the live
    ``ToolRegistry`` the builder retained on the policy. Hand-attaching a
    registry would assert that a hand-attached registry works.

    Returns ``None`` when pydantic-ai isn't installed, so the caller can pin the
    no-registry behaviour instead of silently testing it by accident.
    """
    from pocketpaw.agents.tool_bridge import build_pydantic_ai_tools
    from pocketpaw.config import Settings
    from pocketpaw.tools.policy import ToolPolicy

    policy = ToolPolicy(profile="full")
    settings = Settings(
        pydantic_ai_model="litellm:test-model",
        litellm_api_base="http://localhost:4000",
        litellm_api_key="sk-test",
    )
    if not build_pydantic_ai_tools(settings, backend="pydantic_ai", policy=policy):
        return None
    return SimpleNamespace(get_tool_policy=lambda: policy)


async def _sse_frames(
    monkeypatch, backend_events: list[Any], *, backend: Any = None
) -> list[tuple[str, dict]]:
    """Drive ``_drive_agent_loop`` and return every yielded SSE frame."""

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A", backend=backend)

        def run(self, agent_id, content, session_key, **run_kwargs):
            async def _gen():
                for ev in backend_events:
                    yield ev

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

    out: list[tuple[str, dict]] = []
    async for ev in run_core._drive_agent_loop(
        _scope_ctx(),
        user_content="find the filings",
        attachments_in=None,
        mentions_in=None,
        history=None,
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    ):
        out.append(ev)
    return out


async def _bridge_events(backend_events: list, *, backend: Any = None) -> list:
    """Drive the group/DM bridge over the same events. Mirrors the harness in
    tests/cloud/shared/test_agent_bridge_plan.py so the two are comparable."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    instance = SimpleNamespace(agent_name="Test Agent", backend=backend)
    pool = MagicMock()
    pool.get = AsyncMock(return_value=instance)
    pool.observe = AsyncMock()

    async def fake_run(*_args, **_kwargs):
        for event in backend_events:
            yield event

    pool.run = fake_run

    from pocketpaw_ee.cloud.models.message import Message as _RealMessage

    to_list_mock = AsyncMock(return_value=[])
    limit_mock = MagicMock()
    limit_mock.to_list = to_list_mock
    sort_mock = MagicMock()
    sort_mock.limit = MagicMock(return_value=limit_mock)
    find_mock = MagicMock()
    find_mock.sort = MagicMock(return_value=sort_mock)

    with (
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()) as m_emit,
        patch.multiple(
            _RealMessage,
            create=True,
            group=MagicMock(),
            deleted=MagicMock(),
            createdAt=MagicMock(),
        ),
        patch.object(_RealMessage, "find", MagicMock(return_value=find_mock)),
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-1",
            group_id="group-1",
            workspace_id="ws-1",
            user_message="find the filings",
            group_members=["user-1"],
        )

    return [call.args[0] for call in m_emit.await_args_list if call.args]


# ---------------------------------------------------------------------------
# the streaming surface narrates at all
# ---------------------------------------------------------------------------


async def test_the_tool_start_frame_carries_a_narration(monkeypatch):
    """The headline fix. Before this, ``tool_start`` carried only tool+input and
    the client had nothing to render but the raw name."""
    frames = await _sse_frames(
        monkeypatch, [_tool_use_event("web_search", {"query": "quarterly filings"}), _done()]
    )

    starts = [data for name, data in frames if name == "tool_start"]
    assert len(starts) == 1, frames
    assert starts[0]["narration"], "the streaming surface emitted no narration"


async def test_the_narration_interpolates_an_allowlisted_argument(monkeypatch):
    """``web_search`` allowlists ``query``, so the phrase names the search.

    Asserted on content, not just presence: a bare "Searching the web" would
    satisfy a truthiness check while losing the whole point of the feature.

    Needs a backend that BRIDGED its tools, which is what makes the tool's
    declared ``Narration`` reachable (HTN-2 reads it off the live instance the
    registry holds). That is the configuration this ships on — pydantic_ai,
    openai_agents, google_adk and deep_agents all go through the bridge.
    """
    backend = _bridged_backend()
    if backend is None:
        pytest.skip("pydantic-ai not installed; no bridged registry to resolve")

    frames = await _sse_frames(
        monkeypatch,
        [_tool_use_event("web_search", {"query": "quarterly filings"}), _done()],
        backend=backend,
    )

    narration = next(d for n, d in frames if n == "tool_start")["narration"]
    assert "quarterly filings" in narration, narration


async def test_without_a_bridged_registry_the_phrase_degrades_to_bare(monkeypatch):
    """The claude_agent_sdk case, pinned rather than left to be discovered.

    That backend surfaces its tools over MCP, so ``narration_registry_for``
    finds nothing and a declared phrase is unreachable — the lookup falls
    through to derive-from-name, which never interpolates arguments because a
    NAME carries no ``safe_args`` allowlist saying which are safe to show.

    So the same search reads "Searching the web" there and "Searching the web
    for quarterly filings" on a bridged backend. That is a real gap, not a
    quirk of this fixture, and it is why this asserts the degraded phrase
    explicitly instead of asserting nothing about it.
    """
    frames = await _sse_frames(
        monkeypatch,
        [_tool_use_event("web_search", {"query": "quarterly filings"}), _done()],
        backend=None,
    )

    narration = next(d for n, d in frames if n == "tool_start")["narration"]
    assert narration == "Searching the web", narration
    assert "quarterly filings" not in narration


async def test_the_tool_name_still_travels(monkeypatch):
    """Narration is ADDITIVE. A client keyed on ``tool`` must be unaffected."""
    frames = await _sse_frames(
        monkeypatch, [_tool_use_event("web_search", {"query": "q"}), _done()]
    )

    start = next(d for n, d in frames if n == "tool_start")
    assert start["tool"] == "web_search"
    assert start["input"] == {"query": "q"}


async def test_a_plan_tool_still_yields_no_tool_start(monkeypatch):
    """HTN-5's substitution is not disturbed by adding a field to the branch it
    falls through to."""
    frames = await _sse_frames(
        monkeypatch,
        [
            _tool_use_event(
                "write_plan", {"items": [{"content": "Ship it", "status": "in_progress"}]}
            ),
            _done(),
        ],
    )

    assert [n for n, _ in frames if n == "tool_start"] == []
    assert [n for n, _ in frames if n == "plan_updated"], frames


# ---------------------------------------------------------------------------
# the two surfaces agree — the test that would have caught the original gap
# ---------------------------------------------------------------------------


async def test_both_surfaces_narrate_the_same_call_identically(monkeypatch):
    """One backend event, both paths, same phrase.

    This is the regression guard for the ACTUAL failure: narration existing on
    one surface and silently not the other. Asserting each side independently is
    what let that ship — both suites were green the whole time.
    """
    backend = _bridged_backend()
    if backend is None:
        pytest.skip("pydantic-ai not installed; no bridged registry to resolve")

    def _events():
        return [_tool_use_event("web_search", {"query": "quarterly filings"}), _done()]

    frames = await _sse_frames(monkeypatch, _events(), backend=backend)
    sse = next(d for n, d in frames if n == "tool_start")["narration"]

    emitted = await _bridge_events(_events(), backend=backend)
    ws = next(e.data for e in emitted if e.type == "agent.tool_use")["narration"]

    assert sse == ws, f"streaming says {sse!r}, group/DM says {ws!r}"
    # Pinned against BOTH surfaces degrading together: identical bare phrases
    # would satisfy the equality above while the feature was broken on each.
    assert "quarterly filings" in sse, sse
