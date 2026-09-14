# tests/cloud/runs/test_run_core_partial_reply_survives.py
# Created 2026-09-14 (fix/partial-reply-survives-failed-run). Reproduces the
# reported bug: when a turn fails or is cancelled mid-stream, the assistant text
# the model had ALREADY produced never reaches the agent's context again, so the
# next turn is answered as if the exchange never happened and the user pays to
# regenerate what was already generated.
#
# Where it goes: ``execute_run`` writes an assistant ``Message`` from exactly one
# place - ``_persist_and_complete``. The failed / cancelled / empty-text branches
# return before it, handing ``full_text`` to ``mark_terminal(partial_text=...)``
# instead. That lands on ``ChatRunDoc.partial_text``, which is durable but which
# ``load_history_for_scope`` does not read: it queries the ``Message`` collection
# and nothing else. The reply is stored and unreachable at the same time.
#
# The concierge surface already solved this - ``paw_bar/router.py``'s
# ``_load_concierge_history`` reads ``user_text`` + ``partial_text`` off the run
# docs precisely because anonymous visitors have no ``Message`` rows. Authed chat
# never inherited it.
#
# EXPECTED STATE ON THE UNFIXED TREE: the two ``survives`` tests fail, the two
# characterization tests pass. The characterizations are what prove the failure
# is a reachability bug and not a data-loss bug - do not delete them with the fix.
from __future__ import annotations

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    load_history_for_scope,
)
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio

# The text the model streamed before the turn died. Distinctive so an assertion
# can never pass on some other row's content.
_PARTIAL = "Here are the three options I found:"


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
        content="what are my options?",
        history=[],
        intent=None,
    )


def _ctx() -> ScopeContext:
    """The REAL ScopeContext, not a stub.

    ``session_key_for`` derives the Message ``session_key`` from ``kind.value``,
    ``scope_id`` and ``target_agent_id``, and ``load_history_for_scope`` queries
    with the same formula. A stub that only fakes ``.value`` would still satisfy
    both halves, and the test would pass against a key no production path writes.
    """
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _noop(*a, **k):
    return None


async def _drive(monkeypatch, events, *, cancelled: bool = False) -> ScopeContext:
    """Run ``execute_run`` against a seeded run doc and a real (mongomock) Mongo.

    Deliberately does NOT patch ``_persist_and_complete`` - the whole question is
    whether the real persist path runs, so stubbing it would answer the question
    by assumption.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    await run_service.create_run(_spec())

    ctx = _ctx()

    async def fake_resolve_scope_context(**_):
        return ctx

    async def fake_agent_events(spec, ctx):  # noqa: ARG001
        for name, data in events:
            yield (name, data)

    class _FakePool:
        async def observe(self, *a, **k):
            return None

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    if cancelled:
        # The user hit stop: the cancel flag is what execute_run checks after the
        # agent loop returns.
        await transport.request_cancel("r1")

    await run_core.execute_run(_spec())
    return ctx


def _assistant_lines(history) -> list[str]:
    return [m["content"] for m in history if m["role"] == "assistant"]


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------


async def test_a_failed_run_leaves_its_partial_reply_in_history(monkeypatch, mongo_db):  # noqa: ARG001
    """A turn that crashed AFTER the model started answering must not erase the
    answer from the agent's memory.

    The provider dies mid-stream. ``_PARTIAL`` has already been streamed to the
    browser and is already on the run doc. The next turn rehydrates history from
    Mongo and finds no trace of it, so the agent answers as though it never
    spoke - and the user pays a second time for the same tokens.
    """
    ctx = await _drive(
        monkeypatch,
        [
            ("chunk", {"content": _PARTIAL, "type": "text"}),
            ("error", {"code": "agent.run_failed", "message": "provider exploded"}),
        ],
    )

    history = await load_history_for_scope(ctx)

    assert _PARTIAL in _assistant_lines(history), (
        "the partial reply is on ChatRunDoc.partial_text but load_history_for_scope "
        "reads only the Message collection, so the next turn is answered cold"
    )


async def test_a_cancelled_run_leaves_its_partial_reply_in_history(monkeypatch, mongo_db):  # noqa: ARG001
    """Same loss on the stop button.

    Cancelling is the one case where the user has READ the partial answer and
    deliberately interrupted it - usually because it was already enough. Dropping
    it from history is the most surprising variant of the bug: the agent forgets
    the exact text the user just acted on.
    """
    ctx = await _drive(
        monkeypatch,
        [("chunk", {"content": _PARTIAL, "type": "text"})],
        cancelled=True,
    )

    history = await load_history_for_scope(ctx)

    assert _PARTIAL in _assistant_lines(history), (
        "a cancelled run's partial reply is dropped from history even though the "
        "user saw it and stopped the stream because of it"
    )


# ---------------------------------------------------------------------------
# Characterization - these PASS today and pin why the bug is what it is
# ---------------------------------------------------------------------------


async def test_the_partial_reply_is_durable_on_the_run_doc(monkeypatch, mongo_db):  # noqa: ARG001
    """Passes today. The text is NOT lost - it is written, just not where the
    history reader looks. That is what makes the fix a reachability change rather
    than a capture change, and it is why no data has to be recovered."""
    await _drive(
        monkeypatch,
        [
            ("chunk", {"content": _PARTIAL, "type": "text"}),
            ("error", {"code": "agent.run_failed", "message": "provider exploded"}),
        ],
    )

    doc = await run_service.get_run("r1")
    assert doc.status == "failed"
    assert doc.partial_text == _PARTIAL
    assert doc.assistant_message_id is None, (
        "no assistant Message was written - the run doc is the only copy"
    )


async def test_a_successful_run_does_reach_history(monkeypatch, mongo_db):  # noqa: ARG001
    """Passes today. Proves the harness, the session-key formula and the history
    query all line up - so a red in the two tests above is the bug and not a
    miswired fixture."""
    ctx = await _drive(
        monkeypatch,
        [
            ("chunk", {"content": _PARTIAL, "type": "text"}),
            ("chunk", {"content": " and here is the third.", "type": "text"}),
        ],
    )

    history = await load_history_for_scope(ctx)

    assert _assistant_lines(history) == [f"{_PARTIAL} and here is the third."]
