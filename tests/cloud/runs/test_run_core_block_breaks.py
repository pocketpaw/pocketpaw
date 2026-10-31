"""Text blocks on either side of a tool call or a thought are separate paragraphs.

The agent streams each assistant text block raw. Appending them straight on
produced "…the fallback archive.The build is still queued." in the saved
message, so a reload looked broken. ``execute_run`` now puts a paragraph break
in front of the first chunk of a new block (unless either side already has
whitespace) and sends that same chunk to the live stream, so the live reply and
the persisted one are the same string.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio


def _spec() -> RunSpec:
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
        content="hi",
        history=[],
        intent=None,
    )


async def _noop(*a, **k):
    return None


async def _scope(**_):
    class _Ctx:
        kind = type("K", (), {"value": "session"})()
        scope_id = "s1"
        workspace_id = "w1"
        user_id = "u1"
        target_agent_id = "a1"
        members = ["u1"]
        session_id = None
        intent = None

    return _Ctx()


def _text(content: str) -> tuple[str, dict[str, Any]]:
    return ("chunk", {"content": content, "type": "text"})


TOOL = [
    ("tool_start", {"tool": "Bash", "input": {"command": "ls"}}),
    ("tool_result", {"tool": "Bash", "output": "ok"}),
]


async def _run(monkeypatch, frames: list[tuple[str, dict[str, Any]]]) -> tuple[str, str]:
    """Drive execute_run over ``frames``; return (persisted text, live text)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    captured: dict[str, str] = {}

    async def _events(spec, ctx):
        for frame in frames:
            yield frame

    async def _persist(spec, ctx, full_text, attachments, usage=None, **_):
        captured["text"] = full_text
        return "assistant-msg-1"

    monkeypatch.setattr(run_core, "_iter_agent_events", _events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", _scope)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    live = "".join(e.data.get("content", "") for e in events if e.event == "chunk")
    return captured["text"], live


async def test_text_blocks_around_a_tool_call_get_a_paragraph_break(monkeypatch):
    persisted, live = await _run(
        monkeypatch,
        [
            _text("Refero came back empty, so I'll use the fallback archive."),
            *TOOL,
            _text("The build is still queued."),
            *TOOL,
            _text("Desktop and mobile look right."),
        ],
    )
    assert persisted == (
        "Refero came back empty, so I'll use the fallback archive.\n\n"
        "The build is still queued.\n\nDesktop and mobile look right."
    )
    assert live == persisted


async def test_a_thought_between_text_blocks_also_breaks_them(monkeypatch):
    persisted, live = await _run(
        monkeypatch,
        [
            _text("Checking the plan."),
            ("thinking", {"content": "the user wants bookings"}),
            _text("Building it now."),
        ],
    )
    assert persisted == "Checking the plan.\n\nBuilding it now."
    assert live == persisted


async def test_chunks_of_one_block_are_joined_as_streamed(monkeypatch):
    persisted, live = await _run(monkeypatch, [_text("Hello"), _text(" world"), _text("!")])
    assert persisted == "Hello world!"
    assert live == persisted


async def test_existing_whitespace_is_not_doubled(monkeypatch):
    persisted, live = await _run(
        monkeypatch,
        [_text("First line.\n"), *TOOL, _text("Second."), *TOOL, _text(" Third.")],
    )
    assert persisted == "First line.\nSecond. Third."
    assert live == persisted


async def test_no_break_before_the_first_text(monkeypatch):
    persisted, live = await _run(monkeypatch, [*TOOL, _text("Done.")])
    assert persisted == "Done."
    assert live == persisted
