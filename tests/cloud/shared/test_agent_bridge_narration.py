# Bridge-level tests for humanized tool narration (HTN-1).
# Created: 2026-08-15 — proves a real ``tool_use`` event reaches the wire as
# "Searching the web for quarterly filings" instead of a bare tool name.
#
# The events here use the shape the backends ACTUALLY emit — ``content`` is a
# prose string ("Using web_search...") and ``metadata`` carries the bare name
# plus the call's input (see ``agents/claude_sdk.py`` and ``agents/pydantic_ai.py``).
# Handing the bridge a tidier hand-built event would test the function rather
# than the surface, and would certify a payload no backend ever produces.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _done_event():
    return SimpleNamespace(type="done", content="", metadata={})


def _tool_use_event(name: str, tool_input: dict):
    """A ``tool_use`` event shaped exactly like the SDK backends emit one."""
    return SimpleNamespace(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": tool_input},
    )


async def _emitted_tool_use(events: list) -> list[dict]:
    """Drive ``_run_agent_response`` over ``events`` and return the payloads of
    every ``agent.tool_use`` it emitted."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    instance = SimpleNamespace(agent_name="Test Agent")
    pool = MagicMock()
    pool.get = AsyncMock(return_value=instance)
    pool.observe = AsyncMock()

    async def fake_run(*_args, **_kwargs):
        for event in events:
            yield event

    pool.run = fake_run

    from pocketpaw_ee.cloud.models.message import Message as _RealMessage

    # Beanie isn't initialized in unit tests; stub the history query so the
    # bridge never reaches a real database. The run yields no text, so the
    # bridge short-circuits before the persistence branch.
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
            user_message="what were the quarterly filings?",
            group_members=["user-1"],
        )

    return [
        call.args[0].data
        for call in m_emit.await_args_list
        if call.args and call.args[0].type == "agent.tool_use"
    ]


@pytest.mark.asyncio
async def test_web_search_tool_use_carries_a_humanized_narration():
    """HTN-1's headline behaviour: a real web_search call reaches the wire with
    a plain-language phrase, and the tool name still rides alongside it."""
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("web_search", {"query": "quarterly filings"}),
            _done_event(),
        ]
    )

    assert len(payloads) == 1, f"expected one agent.tool_use emit, got {payloads}"
    payload = payloads[0]

    assert payload["narration"] == "Searching the web for quarterly filings"
    # Additive only — clients keyed on ``tool`` keep working untouched.
    assert payload["tool"] == "web_search"
    assert payload["agent_id"] == "agent-1"
    assert payload["group_id"] == "group-1"


@pytest.mark.asyncio
async def test_unannotated_tool_emits_no_narration_field():
    """HTN-2 owns the derive-from-name fallback. Until then an unannotated tool
    stays silent — the bridge must not invent a phrase of its own."""
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("pocketpaw_sites_publish", {"pocket_id": "p1"}),
            _done_event(),
        ]
    )

    assert len(payloads) == 1
    payload = payloads[0]

    assert payload["tool"] == "pocketpaw_sites_publish"
    assert payload.get("narration") is None


@pytest.mark.asyncio
async def test_missing_query_degrades_to_the_bare_phrase():
    """A tool call whose args don't carry the allowlisted field still narrates
    — just without interpolation."""
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("web_search", {}),
            _done_event(),
        ]
    )

    assert payloads[0]["narration"] == "Searching the web"
    assert payloads[0]["tool"] == "web_search"


@pytest.mark.asyncio
async def test_secrets_in_tool_input_never_reach_the_wire():
    """The bridge hands the whole input dict to the renderer, so this is the
    end-to-end proof that only allowlisted fields survive the trip."""
    payloads = await _emitted_tool_use(
        [
            _tool_use_event(
                "web_search",
                {"query": "quarterly filings", "api_key": "sk-live-SUPERSECRET"},
            ),
            _done_event(),
        ]
    )

    assert payloads[0]["narration"] == "Searching the web for quarterly filings"
    assert "SUPERSECRET" not in str(payloads[0])


@pytest.mark.asyncio
async def test_event_without_metadata_still_reports_a_tool():
    """Backwards compatibility: an event carrying only ``content`` (no
    metadata) keeps falling back to the old content-based extraction."""
    payloads = await _emitted_tool_use(
        [
            SimpleNamespace(type="tool_use", content={"tool": "shell"}, metadata={}),
            _done_event(),
        ]
    )

    assert payloads[0]["tool"] == "shell"
    assert payloads[0].get("narration") is None


@pytest.mark.asyncio
async def test_thinking_event_is_unchanged():
    """The thinking branch shares the AgentToolUse wire type and must not have
    grown a narration field."""
    payloads = await _emitted_tool_use(
        [
            SimpleNamespace(type="thinking", content="pondering", metadata={}),
            _done_event(),
        ]
    )

    assert payloads[0]["tool"] == "thinking"
    assert "narration" not in payloads[0]
