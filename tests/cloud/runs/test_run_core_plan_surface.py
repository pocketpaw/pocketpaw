# tests/cloud/runs/test_run_core_plan_surface.py
# Created 2026-08-15 (HTN-5) — pins the plan surface on the STREAMING chat path.
# The group/DM bridge is covered by tests/cloud/shared/test_agent_bridge_plan.py;
# this file covers the other call site, which most users actually watch.
#
# Two levels, deliberately:
#   * _drive_agent_loop yields a ("plan_updated", payload) frame instead of the
#     ("tool_start", ...) chip, with the payload contents asserted.
#   * execute_run writes that frame to the real SSE transport — proof it reaches
#     a client, not just that the generator produced a tuple. `append_event` has
#     no name whitelist, and this is what pins that.
#
# The backend events use the shape pydantic-ai actually emits: ``content`` is
# the prose "Using write_plan...", ``metadata`` carries the bare name plus the
# call's argument dict (``agents/pydantic_ai.py::_announce_tool``).
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio


THREE_STEP = (
    ("Add the database migration", "completed"),
    ("Wire the endpoint", "in_progress"),
    ("Backfill the existing rows", "pending"),
)


def _write_plan_event(*pairs: tuple[str, str]):
    """A ``write_plan`` call carrying the whole ordered plan, as the tool does."""
    return SimpleNamespace(
        type="tool_use",
        content="Using write_plan...",
        metadata={
            "name": "write_plan",
            "input": {
                "items": [{"content": content, "status": status} for content, status in pairs]
            },
        },
    )


def _tool_use_event(name: str, tool_input: dict):
    return SimpleNamespace(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": tool_input},
    )


def _scope_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _drive_and_collect(
    monkeypatch, backend_events: list[Any], *, emit_stream_start: bool = False
) -> list[tuple[str, dict]]:
    """Drive _drive_agent_loop against a fake pool yielding ``backend_events``."""

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A")

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
    gen = run_core._drive_agent_loop(
        _scope_ctx(),
        user_content="ship the plan surface",
        attachments_in=None,
        mentions_in=None,
        history=None,
        is_cancelled=_never_cancelled,
        emit_stream_start=emit_stream_start,
    )
    async for ev in gen:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# _drive_agent_loop yields the plan frame
# ---------------------------------------------------------------------------


async def test_write_plan_yields_a_plan_frame_with_the_whole_plan(monkeypatch):
    """The headline behaviour on the streaming surface: the ordered plan, its
    progress, and a seq — asserted by contents, not by frame count."""
    out = await _drive_and_collect(
        monkeypatch,
        [_write_plan_event(*THREE_STEP), SimpleNamespace(type="done", content="")],
    )

    plans = [data for name, data in out if name == "plan_updated"]
    assert len(plans) == 1, f"expected one plan_updated frame, got {out}"

    assert plans[0]["items"] == [
        {"id": "1", "content": "Add the database migration", "status": "completed"},
        {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
        {"id": "3", "content": "Backfill the existing rows", "status": "pending"},
    ]
    assert plans[0]["progress"] == {"completed": 1, "total": 3}
    assert plans[0]["seq"] == 1
    assert plans[0]["agent_id"] == "a1"
    assert plans[0]["run_id"], "the panel needs a per-run key to scope itself to"


async def test_a_plan_tool_does_not_also_yield_a_tool_chip(monkeypatch):
    """Same call as the bridge makes: the panel is the narration, so the
    "write_plan" chip would be noise beside it."""
    out = await _drive_and_collect(
        monkeypatch,
        [_write_plan_event(*THREE_STEP), SimpleNamespace(type="done", content="")],
    )
    names = [name for name, _ in out]

    assert "tool_start" not in names, f"plan call also yielded a tool chip: {out}"
    plans = [data for name, data in out if name == "plan_updated"]
    assert [item["content"] for item in plans[0]["items"]] == [
        "Add the database migration",
        "Wire the endpoint",
        "Backfill the existing rows",
    ]


async def test_two_identical_write_plan_calls_yield_one_frame(monkeypatch):
    """``write_plan`` fires at the start AND the end of every step and resends
    the whole list; without coalescing the panel flickers."""
    out = await _drive_and_collect(
        monkeypatch,
        [
            _write_plan_event(*THREE_STEP),
            _write_plan_event(*THREE_STEP),
            SimpleNamespace(type="done", content=""),
        ],
    )

    plans = [data for name, data in out if name == "plan_updated"]
    assert len(plans) == 1, f"expected coalescing to one frame, got {len(plans)}"
    # A count alone would pass if the surviving frame carried a degenerate plan.
    assert plans[0]["items"] == [
        {"id": "1", "content": "Add the database migration", "status": "completed"},
        {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
        {"id": "3", "content": "Backfill the existing rows", "status": "pending"},
    ]
    assert plans[0]["progress"] == {"completed": 1, "total": 3}


async def test_seq_increases_across_genuine_updates(monkeypatch):
    out = await _drive_and_collect(
        monkeypatch,
        [
            _write_plan_event(("Wire the endpoint", "pending")),
            _write_plan_event(("Wire the endpoint", "in_progress")),
            # An identical repeat in the middle must not burn a seq.
            _write_plan_event(("Wire the endpoint", "in_progress")),
            _write_plan_event(("Wire the endpoint", "completed")),
            SimpleNamespace(type="done", content=""),
        ],
    )

    plans = [data for name, data in out if name == "plan_updated"]
    assert [p["seq"] for p in plans] == [1, 2, 3]
    assert [p["items"] for p in plans] == [
        [{"id": "1", "content": "Wire the endpoint", "status": "pending"}],
        [{"id": "1", "content": "Wire the endpoint", "status": "in_progress"}],
        [{"id": "1", "content": "Wire the endpoint", "status": "completed"}],
    ]
    assert [p["progress"] for p in plans] == [
        {"completed": 0, "total": 1},
        {"completed": 0, "total": 1},
        {"completed": 1, "total": 1},
    ]


async def test_cancelled_survives_to_the_frame(monkeypatch):
    out = await _drive_and_collect(
        monkeypatch,
        [
            _write_plan_event(("Add the migration", "completed"), ("Wire it up", "cancelled")),
            SimpleNamespace(type="done", content=""),
        ],
    )

    plans = [data for name, data in out if name == "plan_updated"]
    assert [item["status"] for item in plans[0]["items"]] == ["completed", "cancelled"]
    assert plans[0]["progress"] == {"completed": 1, "total": 2}


async def test_an_unreadable_plan_call_falls_back_to_the_tool_chip(monkeypatch):
    """Fail-open, and it matters more here: pydantic-ai currently announces the
    call with ``input={}`` before the args finish streaming (HTN-9)."""
    out = await _drive_and_collect(
        monkeypatch,
        [_tool_use_event("write_plan", {}), SimpleNamespace(type="done", content="")],
    )
    names = [name for name, _ in out]

    assert "plan_updated" not in names
    chips = [data for name, data in out if name == "tool_start"]
    assert [c["tool"] for c in chips] == ["write_plan"]


async def test_a_non_plan_tool_still_yields_its_chip(monkeypatch):
    """Regression guard: ordinary tools are untouched by the substitution."""
    out = await _drive_and_collect(
        monkeypatch,
        [
            _tool_use_event("web_search", {"query": "quarterly filings"}),
            SimpleNamespace(type="done", content=""),
        ],
    )

    assert [name for name, _ in out if name == "plan_updated"] == []
    chips = [data for name, data in out if name == "tool_start"]
    assert len(chips) == 1
    assert chips[0]["tool"] == "web_search"
    assert chips[0]["input"] == {"query": "quarterly filings"}


async def test_plan_run_id_matches_the_stream_start_frame(monkeypatch):
    """``run_id`` is the id the client already received on ``stream_start``, so
    the panel can attach itself without a second key — the same property the
    group/DM bridge has. This is why ``_new_run_id()`` was hoisted."""
    out = await _drive_and_collect(
        monkeypatch,
        [_write_plan_event(*THREE_STEP), SimpleNamespace(type="done", content="")],
        emit_stream_start=True,
    )

    starts = [data for name, data in out if name == "stream_start"]
    plans = [data for name, data in out if name == "plan_updated"]

    assert starts[0]["run_id"], "stream_start must still carry a run_id"
    assert plans[0]["run_id"] == starts[0]["run_id"]


# ---------------------------------------------------------------------------
# execute_run writes the frame to the real SSE transport
# ---------------------------------------------------------------------------


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
        content="ship it",
        history=[],
        intent=None,
    )


async def _noop(*a, **k):
    return None


async def fake_resolve_scope_context(**_):
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


async def test_plan_frame_reaches_the_sse_stream(monkeypatch):
    """The end-to-end proof: a ``plan_updated`` frame is written to the live
    stream a client reads. ``append_event`` takes any frame name, and this is
    what pins that the plan actually arrives rather than being dropped."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    async def _persist_stub(spec, ctx, full_text, attachments, usage=None):
        return "assistant-msg-1"

    async def fake_agent_events(spec, ctx):
        yield (
            "plan_updated",
            {
                "agent_id": "a1",
                "agent_name": "A",
                "run_id": "stream-run-1",
                "seq": 1,
                "items": [
                    {"id": "1", "content": "Add the database migration", "status": "completed"},
                    {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
                ],
                "progress": {"completed": 1, "total": 2},
            },
        )
        yield ("chunk", {"content": "done", "type": "text"})

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    plans = [e for e in events if e.event == "plan_updated"]

    assert len(plans) == 1, f"plan frame did not reach the stream: {[e.event for e in events]}"
    assert plans[0].data["items"] == [
        {"id": "1", "content": "Add the database migration", "status": "completed"},
        {"id": "2", "content": "Wire the endpoint", "status": "in_progress"},
    ]
    assert plans[0].data["progress"] == {"completed": 1, "total": 2}
    assert plans[0].data["seq"] == 1
