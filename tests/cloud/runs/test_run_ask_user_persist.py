# tests/cloud/runs/test_run_ask_user_persist.py — the pending ask_user question
# survives a reload. A run that emits an ``ask_user_question`` frame stores the
# ask on its assistant Message (``Message.ask_user``), the session history wire
# dict returns it as ``askUser``, and the user's next message in the same scope
# marks it answered. An ask-only turn (no text) still writes a Message, with the
# question as its text, so the chips have a row to render on after a reload.
#
# Persisted shape (``Message.ask_user``):
#   {id, question, options, multi_select, answered, answer}
# Wire shape (``askUser`` on the history dict):
#   {id, question, options, multiSelect, answered, answer}
from __future__ import annotations

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat import message_service
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

_ASK = {"question": "Which vibe?", "options": ["Clean", "Warm"]}


def _spec(run_id: str = "r1") -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="build me a site",
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


async def _run(monkeypatch, events, *, run_id: str = "r1") -> ScopeContext:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec(run_id))

    async def fake_resolve_scope_context(**_):
        return ctx

    async def agent(_spec, _ctx):
        for name, data in events:
            yield (name, data)

    monkeypatch.setattr(run_core, "_iter_agent_events", agent)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec(run_id))
    return ctx


async def _assistants(ctx: ScopeContext) -> list[Message]:
    rows = await Message.find(
        {"session_key": session_key_for(ctx), "role": "assistant", "workspace_id": "w1"}
    ).to_list()
    return sorted(rows, key=lambda m: m.createdAt)


async def _reply(ctx: ScopeContext, content: str) -> None:
    await message_service.persist_user_message_for_scope(
        kind=ctx.kind.value,
        scope_id=ctx.scope_id,
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        session_key=session_key_for(ctx),
        content=content,
    )


async def test_a_run_that_asks_stores_the_pending_ask(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(
        monkeypatch,
        [
            ("ask_user_question", _ASK),
            ("chunk", {"content": "Pick a vibe.", "type": "text"}),
        ],
    )
    [msg] = await _assistants(ctx)
    assert msg.content == "Pick a vibe."
    assert msg.ask_user is not None
    assert msg.ask_user.model_dump() == {
        "id": "r1",
        "question": "Which vibe?",
        "options": ["Clean", "Warm"],
        "multi_select": False,
        "answered": False,
        "answer": None,
    }

    wire = sessions_service._message_to_dict(msg, "assistant")
    assert wire["askUser"] == {
        "id": "r1",
        "question": "Which vibe?",
        "options": ["Clean", "Warm"],
        "multiSelect": False,
        "answered": False,
        "answer": None,
    }


async def test_an_ask_only_turn_still_writes_a_message(monkeypatch, mongo_db):  # noqa: ARG001
    # The agent called ask_user and said nothing else: without a row the chips
    # have nowhere to render after a reload.
    ctx = await _run(monkeypatch, [("ask_user_question", _ASK)])
    [msg] = await _assistants(ctx)
    assert msg.content == "Which vibe?"
    assert msg.ask_user is not None and msg.ask_user.answered is False

    # The agent's own history carries the question it asked.
    history = await load_history_for_scope(ctx)
    assert {"role": "assistant", "content": "Which vibe?"} in history


async def test_the_users_next_message_marks_the_ask_answered(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(monkeypatch, [("ask_user_question", _ASK)])
    await _reply(ctx, "Warm")

    [msg] = await _assistants(ctx)
    assert msg.ask_user is not None
    assert msg.ask_user.answered is True
    assert msg.ask_user.answer == "Warm"
    wire = sessions_service._message_to_dict(msg, "assistant")
    assert wire["askUser"]["answered"] is True
    assert wire["askUser"]["answer"] == "Warm"


async def test_a_typed_reply_also_answers_the_ask(monkeypatch, mongo_db):  # noqa: ARG001
    # Any reply ends the pending question: the chips must not come back after a
    # reload once the user moved on, chip or not.
    ctx = await _run(monkeypatch, [("ask_user_question", _ASK)])
    await _reply(ctx, "something else entirely")
    [msg] = await _assistants(ctx)
    assert msg.ask_user is not None and msg.ask_user.answered is True


async def test_the_latest_ask_wins_when_a_run_asks_twice(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(
        monkeypatch,
        [
            ("ask_user_question", _ASK),
            ("ask_user_question", {"question": "Which tone?", "options": ["Calm", "Bold"]}),
        ],
    )
    [msg] = await _assistants(ctx)
    assert msg.ask_user is not None
    assert msg.ask_user.question == "Which tone?"
    assert msg.ask_user.options == ["Calm", "Bold"]


async def test_a_plain_run_has_no_ask_on_the_message_or_the_wire(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(monkeypatch, [("chunk", {"content": "Hi there.", "type": "text"})])
    [msg] = await _assistants(ctx)
    assert msg.ask_user is None
    assert "askUser" not in sessions_service._message_to_dict(msg, "assistant")


async def test_a_reply_with_no_pending_ask_changes_nothing(monkeypatch, mongo_db):  # noqa: ARG001
    ctx = await _run(monkeypatch, [("chunk", {"content": "Hi there.", "type": "text"})])
    await _reply(ctx, "thanks")
    [msg] = await _assistants(ctx)
    assert msg.ask_user is None
