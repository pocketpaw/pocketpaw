# tests/cloud/runs/test_run_tool_steps.py
# Created 2026-09-28 (feat/persist-tool-steps). Reproduces: tool calls, tool
# results and thinking a cloud chat run streams are gone after a refresh. They
# only ever reach the Redis run stream (1h TTL); the assistant Message stores the
# text and ripple/artifact attachments, and nothing else.
#
# The contract these tests pin (the implementation must match it):
#   * ``Message.steps: list[MessageStep]`` — ordered, one entry per thinking block
#     or tool call, plus ``Message.steps_omitted: int`` when caps drop some.
#   * ``MessageStep`` fields: ``id``, ``kind`` ("thinking" | "tool"), ``tool``,
#     ``narration``, ``input``, ``output``, ``output_truncated``, ``status``
#     ("running" | "complete" | "error" | "missing_result"), ``started_at``,
#     ``ended_at``, and ``text`` for thinking.
#   * Written with the Message on EVERY path that writes one: completed, and the
#     partial (failed / cancelled / interrupted) paths from fix/chat-run-heartbeat.
#   * Tool input is scrubbed, tool output and thinking are redacted and capped.
#   * The provisional ``input_pending`` announcement the claude_sdk backend makes
#     before the real one is ONE step, not two.
#   * The UI history wire dict carries ``steps`` (camelCase inner keys); the LLM
#     history does not.
#
# EXPECTED STATE ON THE UNFIXED TREE: every test fails except the last
# characterization.
from __future__ import annotations

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    load_history_for_scope,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.models.message import Message
from pocketpaw_ee.cloud.sessions import service as sessions_service

pytestmark = pytest.mark.asyncio

_SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 3


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
        content="look it up",
        history=[],
        intent=None,
    )


def _ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _noop(*_a, **_k):
    return None


class _FakePool:
    async def observe(self, *_a, **_k):
        return None


async def _run(monkeypatch, events, *, cancel: bool = False) -> ScopeContext:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def fake_resolve_scope_context(**_):
        return ctx

    async def agent(_spec, _ctx):
        for name, data in events:
            if name == "__cancel__":  # the user hits stop at this point
                await transport.request_cancel("r1")
                continue
            yield (name, data)

    monkeypatch.setattr(run_core, "_iter_agent_events", agent)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    if cancel:
        await transport.request_cancel("r1")

    await run_core.execute_run(_spec())
    return ctx


async def _assistant(ctx: ScopeContext) -> Message:
    rows = await Message.find(
        {"session_key": session_key_for(ctx), "role": "assistant", "workspace_id": "w1"}
    ).to_list()
    assert len(rows) == 1, f"expected one assistant Message, got {len(rows)}"
    return rows[0]


_SEARCH = [
    ("thinking", {"content": "The user wants the latest release. "}),
    ("thinking", {"content": "I'll search for it."}),
    # claude_sdk's provisional announcement, then the real one for the same call.
    ("tool_start", {"tool": "web_search", "input": {}, "input_pending": True}),
    (
        "tool_start",
        {
            "tool": "web_search",
            "input": {"query": "pocketpaw release"},
            "narration": "Searching the web for pocketpaw release",
        },
    ),
    ("tool_result", {"tool": "web_search", "output": "v0.4.18 released on Sept 20"}),
    ("chunk", {"content": "The latest release is v0.4.18.", "type": "text"}),
]


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------


async def test_a_completed_run_saves_its_steps_on_the_message(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(monkeypatch, _SEARCH)

    msg = await _assistant(ctx)
    assert msg.content == "The latest release is v0.4.18."
    kinds = [s.kind for s in msg.steps]
    assert kinds == ["thinking", "tool"], (
        "thinking must merge into one block and the provisional input_pending "
        f"announcement must not become its own step; got {kinds}"
    )

    thinking, tool = msg.steps
    assert thinking.text == "The user wants the latest release. I'll search for it."
    assert tool.tool == "web_search"
    assert tool.input == {"query": "pocketpaw release"}
    assert tool.narration == "Searching the web for pocketpaw release"
    assert tool.output == "v0.4.18 released on Sept 20"
    assert tool.output_truncated is False
    assert tool.status == "complete"
    assert tool.started_at is not None and tool.ended_at is not None
    assert tool.ended_at >= tool.started_at
    assert len({s.id for s in msg.steps}) == 2, "step ids must be unique"


async def test_results_pair_with_the_oldest_open_call_of_the_same_tool(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """No tool-call id reaches run_core, so pairing is by name. Two calls to the
    same tool that run back to back must each get their own result, in order."""
    ctx = await _run(
        monkeypatch,
        [
            ("tool_start", {"tool": "read_file", "input": {"path": "a.py"}}),
            ("tool_start", {"tool": "read_file", "input": {"path": "b.py"}}),
            ("tool_result", {"tool": "read_file", "output": "contents of a"}),
            ("tool_result", {"tool": "read_file", "output": "contents of b"}),
            ("chunk", {"content": "Both read.", "type": "text"}),
        ],
    )

    steps = (await _assistant(ctx)).steps
    assert [(s.input, s.output) for s in steps] == [
        ({"path": "a.py"}, "contents of a"),
        ({"path": "b.py"}, "contents of b"),
    ]


async def test_a_failed_run_keeps_its_steps_and_marks_the_open_call(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """The long-run bug's other half: a run that dies mid-tool keeps what it did."""
    ctx = await _run(
        monkeypatch,
        [
            ("tool_start", {"tool": "run_tests", "input": {"path": "tests/"}}),
            ("tool_result", {"tool": "run_tests", "output": "412 passed"}),
            ("chunk", {"content": "Tests pass. Now deploying", "type": "text"}),
            ("tool_start", {"tool": "deploy", "input": {"env": "staging"}}),
            ("error", {"code": "agent.run_failed", "message": "provider exploded"}),
        ],
    )

    msg = await _assistant(ctx)
    assert msg.run_status == "failed"
    assert [(s.tool, s.status) for s in msg.steps] == [
        ("run_tests", "complete"),
        ("deploy", "missing_result"),
    ]
    assert msg.steps[1].ended_at is not None


async def test_a_cancelled_run_keeps_its_steps(monkeypatch, mongo_db):  # noqa: ARG001
    """The user stops mid-tool. The finished call and the one in flight both stay."""
    ctx = await _run(
        monkeypatch,
        [
            ("tool_start", {"tool": "list_repos", "input": {}}),
            ("tool_result", {"tool": "list_repos", "output": "12 repos"}),
            ("chunk", {"content": "Auditing 12 repos.", "type": "text"}),
            ("tool_start", {"tool": "audit_repo", "input": {"repo": "api"}}),
            ("__cancel__", None),
            ("tool_result", {"tool": "audit_repo", "output": "never recorded"}),
        ],
    )
    msg = await _assistant(ctx)
    assert msg.run_status == "cancelled"
    assert [(s.tool, s.status) for s in msg.steps] == [
        ("list_repos", "complete"),
        ("audit_repo", "missing_result"),
    ]


async def test_secrets_are_scrubbed_and_large_output_is_capped(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(
        monkeypatch,
        [
            (
                "tool_start",
                {"tool": "http_get", "input": {"url": "https://x.dev", "api_key": _SECRET}},
            ),
            ("tool_result", {"tool": "http_get", "output": f"token={_SECRET}\n" + "x" * 50_000}),
            ("chunk", {"content": "Fetched.", "type": "text"}),
        ],
    )

    step = (await _assistant(ctx)).steps[0]
    assert _SECRET not in repr(step.input), "secret-named tool args must be masked"
    assert step.input["url"] == "https://x.dev"
    assert _SECRET not in step.output, "secrets in tool output must be redacted"
    assert step.output_truncated is True
    assert len(step.output) <= 4096 + 64, f"output was stored at {len(step.output)} chars"


async def test_a_run_with_too_many_steps_keeps_a_bounded_list(monkeypatch, mongo_db):  # noqa: ARG001
    events = []
    for i in range(120):
        events.append(("tool_start", {"tool": "grep", "input": {"pattern": f"p{i}"}}))
        events.append(("tool_result", {"tool": "grep", "output": f"hit {i}"}))
    events.append(("chunk", {"content": "Done searching.", "type": "text"}))

    msg = await _assistant(await _run(monkeypatch, events))

    assert 0 < len(msg.steps) <= 50
    assert msg.steps_omitted == 120 - len(msg.steps)


async def test_ui_history_returns_steps_but_llm_history_does_not(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(monkeypatch, _SEARCH)
    msg = await _assistant(ctx)

    wire = sessions_service._message_to_dict(msg, "assistant")
    assert [s["kind"] for s in wire["steps"]] == ["thinking", "tool"]
    tool = wire["steps"][1]
    assert tool["tool"] == "web_search"
    assert tool["status"] == "complete"
    assert "startedAt" in tool and "endedAt" in tool and "outputTruncated" in tool

    history = await load_history_for_scope(ctx)
    assert all(set(row) == {"role", "content"} for row in history), (
        "tool steps must never be replayed into the model's context"
    )


# ---------------------------------------------------------------------------
# Characterization — passes today
# ---------------------------------------------------------------------------


async def test_a_plain_text_run_writes_no_steps_key_on_the_wire(monkeypatch, mongo_db):  # noqa: ARG001
    """Messages without steps keep today's wire payload exactly."""
    ctx = await _run(monkeypatch, [("chunk", {"content": "Hi there.", "type": "text"})])
    wire = sessions_service._message_to_dict(await _assistant(ctx), "assistant")
    assert "steps" not in wire
