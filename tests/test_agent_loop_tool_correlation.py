# tests/test_agent_loop_tool_correlation.py
# Created: 2026-08-15 (HTN-4 follow-up, feat/claude-sdk-tool-args) — pins the
# correlation contract between the loop's ``tool_start`` and ``tool_result``
# SystemEvents, now that a streamed claude_sdk call announces itself TWICE.
#
# HTN-4 made the claude_sdk backend emit two ``tool_use`` events per streamed
# call: a provisional one when the tool block opens (``input_pending=True``,
# arguments not yet streamed) and a resolved one carrying the assembled
# arguments. The loop appended one ``pending_tool_calls`` entry per EVENT, so
# each streamed call left a stale entry behind. ``tool_result`` pops the first
# entry matching by name, so the residue accumulated across a turn and the
# ``pop(0)`` fallback could hand a later unmatched result an id belonging to an
# earlier call. It also published two ``tool_start`` events per call, the first
# with empty ``params`` — and both the dashboard transparency log
# (frontend/js/features/transparency.js) and the client activity store
# (client/src/lib/stores/activity.svelte.ts) APPEND those rather than replacing,
# so the empty one was a visible junk row, not a harmless duplicate.
#
# These tests drive ``AgentLoop._process_message`` with a scripted backend event
# stream and assert on the SystemEvents published to the bus — the correlation is
# only observable there, since ``pending_tool_calls`` is a local. The harness
# mirrors ``tests/test_agent_loop.py``.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.bus import Channel, InboundMessage
from pocketpaw.prompt import AssembledPrompt

_STUB_DIGEST = "0123456789abcdef"

_WRITE_ARGS = {"file_path": "/tmp/a.md", "content": "hello"}
_READ_ARGS = {"file_path": "/tmp/b.md"}


# ===========================================================================
# Event-script helpers — one per shape of backend emission
# ===========================================================================


def _provisional(name: str) -> AgentEvent:
    """What claude_sdk emits when the tool block opens: name, no arguments."""
    return AgentEvent(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": {}, "input_pending": True},
    )


def _resolved(name: str, args: dict) -> AgentEvent:
    """What claude_sdk emits once the message completes: the real arguments."""
    return AgentEvent(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": args, "input_pending": False},
    )


def _unflagged(name: str, args: dict) -> AgentEvent:
    """What every OTHER backend emits — no ``input_pending`` key at all."""
    return AgentEvent(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": args},
    )


def _result(name: str, content: str = "ok") -> AgentEvent:
    return AgentEvent(type="tool_result", content=content, metadata={"name": name})


# ===========================================================================
# Harness
# ===========================================================================


async def _drive(events: list[AgentEvent]) -> list:
    """Run one turn whose backend yields ``events``; return the SystemEvents."""
    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()

    memory = MagicMock()
    memory.add_to_session = AsyncMock()
    memory.get_session_history = AsyncMock(return_value=[])
    memory.get_compacted_history = AsyncMock(return_value=[])
    memory.resolve_session_key = AsyncMock(side_effect=lambda k: k)

    router = MagicMock()

    async def _run(
        message,
        *,
        system_prompt=None,
        history=None,
        session_key=None,
        system_prompt_digest="",
    ):
        for ev in events:
            yield ev
        yield AgentEvent(type="done", content="")

    router.run = _run
    router.stop = AsyncMock()

    settings = MagicMock()
    settings.agent_backend = "claude_agent_sdk"
    settings.max_concurrent_conversations = 5

    with (
        patch("pocketpaw.agents.loop.get_message_bus", return_value=bus),
        patch("pocketpaw.agents.loop.get_memory_manager", return_value=memory),
        patch("pocketpaw.agents.loop.AgentContextBuilder") as builder_cls,
        patch("pocketpaw.agents.loop.AgentRouter", return_value=router),
        patch("pocketpaw.agents.loop.get_settings", return_value=settings),
        patch("pocketpaw.agents.loop.Settings") as settings_cls,
    ):
        builder_cls.return_value.assemble_system_prompt = AsyncMock(
            return_value=AssembledPrompt(text="System Prompt", stable_digest=_STUB_DIGEST)
        )
        settings_cls.load.return_value = settings

        loop = AgentLoop()
        await loop._process_message(
            InboundMessage(
                channel=Channel.CLI,
                sender_id="user1",
                chat_id="chat1",
                content="run a tool",
            )
        )

    return [call[0][0] for call in bus.publish_system.call_args_list]


def _of_type(system_events, event_type: str) -> list[dict]:
    return [e.data for e in system_events if e.event_type == event_type]


# ===========================================================================
# 1. One streamed call -> one tool_start, real params, correct correlation
# ===========================================================================


async def test_streamed_call_yields_one_tool_start_with_real_params() -> None:
    """A streamed call arrives as provisional + resolved. Only the resolved one
    may become a pending entry, so the turn publishes ONE ``tool_start`` carrying
    the real arguments, and the result correlates to it."""
    system_events = await _drive(
        [
            _provisional("Write"),
            _resolved("Write", _WRITE_ARGS),
            _result("Write"),
        ]
    )

    starts = _of_type(system_events, "tool_start")
    results = _of_type(system_events, "tool_result")

    assert len(starts) == 1, (
        f"one real call must publish one tool_start, got {len(starts)}: "
        f"{[s.get('params') for s in starts]}"
    )
    assert starts[0]["params"] == _WRITE_ARGS, (
        "the published tool_start must carry the REAL arguments, not the "
        f"provisional placeholder — got {starts[0]['params']}"
    )
    assert len(results) == 1
    assert results[0]["tool_call_id"] == starts[0]["tool_call_id"], (
        "the tool_result must correlate to the tool_start of its own call"
    )
    assert results[0]["tool_call_id"], "correlation id must not be empty"


# ===========================================================================
# 2. Multi-tool turn leaves no stale pending entries
# ===========================================================================


async def test_multi_tool_streamed_turn_leaves_no_stale_pending_entries() -> None:
    """The accumulation test. Two streamed calls to the SAME tool, both resolved
    by their results, must leave the pending list EMPTY.

    ``pending_tool_calls`` is a local, so residue is probed through the behaviour
    it drives: a trailing result whose name matches nothing falls back to
    ``pop(0)``. With an empty list that yields an empty id; with leftovers it
    yields an id belonging to an earlier call, which is exactly the corruption
    under test."""
    system_events = await _drive(
        [
            _provisional("Write"),
            _resolved("Write", _WRITE_ARGS),
            _result("Write"),
            _provisional("Write"),
            _resolved("Write", _READ_ARGS),
            _result("Write"),
            # Probe: a result for a call that never started.
            _result("Ghost"),
        ]
    )

    starts = _of_type(system_events, "tool_start")
    results = _of_type(system_events, "tool_result")

    assert len(starts) == 2, f"two real calls must publish two tool_starts, got {len(starts)}"
    assert [s["params"] for s in starts] == [_WRITE_ARGS, _READ_ARGS], (
        "each tool_start must carry its own call's real arguments"
    )

    assert results[0]["tool_call_id"] == starts[0]["tool_call_id"]
    assert results[1]["tool_call_id"] == starts[1]["tool_call_id"]

    ghost = results[2]
    assert ghost["tool_call_id"] == "", (
        "after both calls are correlated the pending list must be EMPTY, so an "
        "unmatched result gets no id. A non-empty id here means stale entries "
        f"accumulated and were handed to the wrong call — got {ghost['tool_call_id']!r}"
    )


# ===========================================================================
# 3. Out-of-order results still correlate to the right call
# ===========================================================================


async def test_interleaved_results_correlate_to_the_right_call() -> None:
    """Two different tools started, results arriving in the reverse order: each
    result must carry the id of ITS OWN start, not the first pending entry."""
    system_events = await _drive(
        [
            _provisional("Read"),
            _resolved("Read", _READ_ARGS),
            _provisional("Write"),
            _resolved("Write", _WRITE_ARGS),
            _result("Write"),
            _result("Read"),
        ]
    )

    starts = _of_type(system_events, "tool_start")
    results = _of_type(system_events, "tool_result")

    assert len(starts) == 2, f"expected two tool_starts, got {len(starts)}"
    ids = {s["name"]: s["tool_call_id"] for s in starts}

    assert results[0]["name"] == "Write"
    assert results[0]["tool_call_id"] == ids["Write"], (
        "the Write result must correlate to the Write call even though Read started first"
    )
    assert results[1]["name"] == "Read"
    assert results[1]["tool_call_id"] == ids["Read"]


# ===========================================================================
# 4. Backends that never set the flag are untouched
# ===========================================================================


async def test_backend_without_input_pending_is_unchanged() -> None:
    """pydantic_ai, deep_agents and friends emit one tool_use per call and never
    set ``input_pending``, so the loop reads None. Absent must behave exactly as
    present-and-False: one start, real params, correct correlation."""
    system_events = await _drive(
        [
            _unflagged("Read", _READ_ARGS),
            _result("Read"),
            _unflagged("Write", _WRITE_ARGS),
            _result("Write"),
            _result("Ghost"),
        ]
    )

    starts = _of_type(system_events, "tool_start")
    results = _of_type(system_events, "tool_result")

    assert len(starts) == 2, f"expected one tool_start per call, got {len(starts)}"
    assert [s["params"] for s in starts] == [_READ_ARGS, _WRITE_ARGS]
    assert results[0]["tool_call_id"] == starts[0]["tool_call_id"]
    assert results[1]["tool_call_id"] == starts[1]["tool_call_id"]
    assert results[2]["tool_call_id"] == "", "an unflagged backend must leave no residue either"


async def test_non_streaming_claude_sdk_path_is_unchanged() -> None:
    """The non-streaming claude_sdk path emits a single resolved event
    (``input_pending=False``) and no provisional one. It must correlate exactly
    as it did before the flag existed."""
    system_events = await _drive(
        [
            _resolved("Write", _WRITE_ARGS),
            _result("Write"),
        ]
    )

    starts = _of_type(system_events, "tool_start")
    results = _of_type(system_events, "tool_result")

    assert len(starts) == 1
    assert starts[0]["params"] == _WRITE_ARGS
    assert results[0]["tool_call_id"] == starts[0]["tool_call_id"]


# ===========================================================================
# 5. Zero-argument and argument-less calls report no arguments, not the metadata
# ===========================================================================


async def test_zero_argument_call_publishes_empty_params() -> None:
    """A tool called with no arguments reports ``input={}``, which is falsy. The
    published ``params`` must be that empty dict — not the metadata dict, which an
    ``or meta`` fallback used to substitute, putting ``{'input': {}, 'name':
    'get_system_status'}`` where the arguments belong in the transparency log."""
    system_events = await _drive(
        [
            AgentEvent(
                type="tool_use",
                content="Using get_system_status...",
                metadata={"name": "get_system_status", "input": {}},
            ),
            _result("get_system_status"),
        ]
    )

    starts = _of_type(system_events, "tool_start")

    assert len(starts) == 1
    assert starts[0]["params"] == {}, (
        "a zero-argument call must publish empty params, not the metadata dict — "
        f"got {starts[0]['params']}"
    )


async def test_backend_omitting_input_publishes_empty_params() -> None:
    """opencode emits ``metadata={"name": ...}`` with no ``input`` key at all. It
    has no arguments to give, so the loop must say so rather than pass the
    metadata dict off as the call's parameters."""
    system_events = await _drive(
        [
            AgentEvent(type="tool_use", content="Using Read...", metadata={"name": "Read"}),
            _result("Read"),
        ]
    )

    starts = _of_type(system_events, "tool_start")

    assert len(starts) == 1
    assert starts[0]["name"] == "Read"
    assert starts[0]["params"] == {}, (
        f"a backend omitting 'input' must publish empty params, got {starts[0]['params']}"
    )
