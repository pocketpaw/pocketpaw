# Bridge-level tests for the agent plan surface (HTN-5).
# Created: 2026-08-15 — proves a real ``write_plan`` call reaches the wire as
# one ``agent.plan_updated`` carrying the whole ordered plan, and that a plan
# tool no longer shows up as a bare ``agent.tool_use`` chip.
#
# The events here use the shape pydantic-ai ACTUALLY emits (``content`` is the
# prose "Using write_plan...", ``metadata`` carries the bare name plus the
# call's argument dict — see ``agents/pydantic_ai.py::_announce_tool``). Handing
# the bridge a tidier hand-built event would test the function rather than the
# surface.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _done_event():
    return SimpleNamespace(type="done", content="", metadata={})


def _tool_use_event(name: str, tool_input: dict):
    return SimpleNamespace(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": tool_input},
    )


def _write_plan_event(*pairs: tuple[str, str]):
    """A ``write_plan`` call carrying the whole ordered plan, as the tool does."""
    return _tool_use_event(
        "write_plan",
        {"items": [{"content": content, "status": status} for content, status in pairs]},
    )


THREE_STEP = (
    ("Add the database migration", "completed"),
    ("Wire the endpoint", "in_progress"),
    ("Backfill the existing rows", "pending"),
)


async def _emitted(events: list) -> list:
    """Drive ``_run_agent_response`` over ``events`` and return every emitted
    event object (type + data), in order."""
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
            user_message="ship the plan surface",
            group_members=["user-1"],
        )

    return [call.args[0] for call in m_emit.await_args_list if call.args]


async def _of_type(events: list, wire_type: str) -> list[dict]:
    return [e.data for e in await _emitted(events) if e.type == wire_type]


@pytest.mark.asyncio
async def test_a_write_plan_call_emits_one_plan_event_with_the_whole_plan():
    """HTN-5's headline behaviour: the ordered plan, its progress, and a seq."""
    payloads = await _of_type([_write_plan_event(*THREE_STEP), _done_event()], "agent.plan_updated")

    assert len(payloads) == 1, f"expected one agent.plan_updated, got {payloads}"
    payload = payloads[0]

    assert payload["items"] == [
        {"id": "1", "content": "Add the database migration", "status": "completed"},
        {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
        {"id": "3", "content": "Backfill the existing rows", "status": "pending"},
    ]
    assert payload["progress"] == {"completed": 1, "total": 3}
    assert payload["seq"] == 1
    assert payload["group_id"] == "group-1"
    assert payload["agent_id"] == "agent-1"
    assert payload["run_id"], "the panel needs a per-run key to scope itself to"


@pytest.mark.asyncio
async def test_event_keys_match_the_sse_frame_apart_from_group_id():
    """Cross-channel parity, pinned so it cannot drift into two shapes.

    The panel runs ONE reconciler over both channels, so the payloads have to
    stay identical. The mirror of this assertion is in
    ``tests/cloud/runs/test_run_core_plan_surface.py`` — the only difference is
    ``group_id``, which this path carries and the streaming chat path
    structurally cannot. If either side gains or loses a field, one of the two
    tests fails.
    """
    payloads = await _of_type([_write_plan_event(*THREE_STEP), _done_event()], "agent.plan_updated")

    assert set(payloads[0]) == {
        "group_id",
        "agent_id",
        "agent_name",
        "run_id",
        "seq",
        "items",
        "progress",
    }


@pytest.mark.asyncio
async def test_a_plan_tool_does_not_also_emit_a_tool_chip():
    """The panel IS the narration; "Using write_plan..." next to it is noise."""
    emitted = await _emitted([_write_plan_event(*THREE_STEP), _done_event()])
    types = [e.type for e in emitted]

    assert "agent.tool_use" not in types, f"plan call also emitted a tool chip: {types}"
    # Assert the plan event is real, not merely present — a substitution that
    # suppressed the chip and emitted an empty plan would satisfy the negative
    # assertion above on its own.
    plans = [e.data for e in emitted if e.type == "agent.plan_updated"]
    assert len(plans) == 1
    assert [item["content"] for item in plans[0]["items"]] == [
        "Add the database migration",
        "Wire the endpoint",
        "Backfill the existing rows",
    ]


@pytest.mark.asyncio
async def test_two_identical_write_plan_calls_emit_one_event():
    """``write_plan`` fires at the start AND the end of every step and resends
    the whole list, so without change-coalescing the panel flickers."""
    payloads = await _of_type(
        [_write_plan_event(*THREE_STEP), _write_plan_event(*THREE_STEP), _done_event()],
        "agent.plan_updated",
    )

    assert len(payloads) == 1, f"expected coalescing to one event, got {len(payloads)}"
    # A count alone would also pass if the surviving event carried a wrong or
    # degenerate plan, so pin its contents: the point is that the ONE event
    # that got through is the full plan, not merely that one event got through.
    assert payloads[0]["items"] == [
        {"id": "1", "content": "Add the database migration", "status": "completed"},
        {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
        {"id": "3", "content": "Backfill the existing rows", "status": "pending"},
    ]
    assert payloads[0]["progress"] == {"completed": 1, "total": 3}


@pytest.mark.asyncio
async def test_seq_increases_across_genuine_updates_in_a_run():
    payloads = await _of_type(
        [
            _write_plan_event(("Wire the endpoint", "pending")),
            _write_plan_event(("Wire the endpoint", "in_progress")),
            # An identical repeat in the middle must not burn a seq.
            _write_plan_event(("Wire the endpoint", "in_progress")),
            _write_plan_event(("Wire the endpoint", "completed")),
            _done_event(),
        ],
        "agent.plan_updated",
    )

    assert [p["seq"] for p in payloads] == [1, 2, 3]
    assert [p["items"][0]["status"] for p in payloads] == ["pending", "in_progress", "completed"]
    # The content must survive every update, not just the status field.
    assert [p["items"] for p in payloads] == [
        [{"id": "1", "content": "Wire the endpoint", "status": "pending"}],
        [{"id": "1", "content": "Wire the endpoint", "status": "in_progress"}],
        [{"id": "1", "content": "Wire the endpoint", "status": "completed"}],
    ]
    assert [p["progress"] for p in payloads] == [
        {"completed": 0, "total": 1},
        {"completed": 0, "total": 1},
        {"completed": 1, "total": 1},
    ]


@pytest.mark.asyncio
async def test_cancelled_survives_the_trip_to_the_wire():
    payloads = await _of_type(
        [
            _write_plan_event(
                ("Add the migration", "completed"), ("Wire the endpoint", "cancelled")
            ),
            _done_event(),
        ],
        "agent.plan_updated",
    )

    assert [item["status"] for item in payloads[0]["items"]] == ["completed", "cancelled"]
    assert payloads[0]["progress"] == {"completed": 1, "total": 2}


@pytest.mark.asyncio
async def test_run_id_matches_the_streaming_message_id():
    """``run_id`` is the bridge's per-run identity, which is the same id
    ``agent.stream_start`` already published as ``message_id`` — so the panel
    can attach itself to the streaming message without a second key."""
    emitted = await _emitted([_write_plan_event(*THREE_STEP), _done_event()])

    starts = [e.data for e in emitted if e.type == "agent.stream_start"]
    plans = [e.data for e in emitted if e.type == "agent.plan_updated"]

    assert plans[0]["run_id"] == starts[0]["message_id"]


@pytest.mark.asyncio
async def test_an_unreadable_plan_call_falls_back_to_the_tool_chip():
    """Fail-open. pydantic-ai currently announces the call with ``input={}``
    before the args finish streaming (HTN-9), so suppressing the chip on an
    empty plan would leave the surface silent instead of degraded."""
    emitted = await _emitted([_tool_use_event("write_plan", {}), _done_event()])
    types = [e.type for e in emitted]

    assert "agent.plan_updated" not in types
    tool_uses = [e.data for e in emitted if e.type == "agent.tool_use"]
    assert [p["tool"] for p in tool_uses] == ["write_plan"]


@pytest.mark.asyncio
async def test_a_non_plan_tool_is_untouched():
    """Regression guard on HTN-1: ordinary tools still emit their chip and
    their narration, and never emit a plan event."""
    emitted = await _emitted(
        [_tool_use_event("web_search", {"query": "quarterly filings"}), _done_event()]
    )

    assert [e.type for e in emitted if e.type == "agent.plan_updated"] == []
    tool_uses = [e.data for e in emitted if e.type == "agent.tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["tool"] == "web_search"
    assert tool_uses[0]["narration"] == "Searching the web for quarterly filings"
