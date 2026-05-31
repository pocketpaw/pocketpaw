"""Regression: backend restart / pool eviction must not wipe agent memory.

The agent backends keep in-process conversation state keyed by ``session_key``.
That state doesn't survive a backend restart or an ``AgentPool`` LRU eviction,
so the POST endpoint must rehydrate history from the persisted ``Message``
collection and ship it on the ``RunSpec`` to the executor. Before this fix,
``history=None`` was passed unconditionally and the agent replied with no
memory of prior turns after any process restart.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as router_mod
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    load_history_for_scope,
    session_key_for,
)


class _StubTransport:
    """Yields one ``stream_end`` so the SSE generator finishes immediately."""

    async def request_cancel(self, run_id: str) -> None:  # noqa: ARG002
        return None

    def read_events(self, run_id: str, *, after: str = "0", block_ms: int = 15000) -> AsyncIterator:  # noqa: ARG002
        async def _gen() -> AsyncIterator:
            from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent

            yield StreamEvent(
                entry_id="1-0",
                event="stream_end",
                data={"assistant_message_id": None, "usage": {}, "cancelled": False},
            )

        return _gen()


# ---------------------------------------------------------------------------
# Helper: the message-collection accessor we patch. Stubs stand in for Beanie
# documents — only the attributes the helper actually reads are populated.
# ---------------------------------------------------------------------------


class _StubMsg:
    def __init__(self, *, role=None, sender_type="user", content=""):
        self.role = role
        self.sender_type = sender_type
        self.content = content


class _StubFindChain:
    """Mimics Beanie's find(...).sort(...).limit(...).to_list() chain."""

    def __init__(self, captured: dict, docs: list[_StubMsg]):
        self._captured = captured
        self._docs = docs

    def sort(self, *args, **kwargs):
        self._captured["sorted"] = True
        return self

    def limit(self, n):
        self._captured["limit"] = n
        return self

    async def to_list(self):
        return list(self._docs)


class _StubMessageModel:
    _captured: dict = {}
    _docs: list[_StubMsg] = []

    @classmethod
    def configure(cls, docs: list[_StubMsg]) -> dict:
        cls._captured = {}
        cls._docs = docs
        return cls._captured

    @classmethod
    def find(cls, query):
        cls._captured["query"] = query
        return _StubFindChain(cls._captured, cls._docs)


# ---------------------------------------------------------------------------
# Unit tests for load_history_for_scope
# ---------------------------------------------------------------------------


def _session_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )


def _group_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )


@pytest.mark.asyncio
async def test_load_history_for_session_scope_uses_session_key(monkeypatch):
    captured = _StubMessageModel.configure(
        [
            _StubMsg(role="user", content="first thing"),
            _StubMsg(role="assistant", content="first reply"),
            _StubMsg(role="user", content="second thing"),
        ]
    )
    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _StubMessageModel)

    ctx = _session_ctx()
    result = await load_history_for_scope(ctx, limit=25)

    assert captured["query"] == {
        "context_type": "session",
        "session_key": session_key_for(ctx),
    }
    assert captured["limit"] == 25
    assert result == [
        {"role": "user", "content": "first thing"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second thing"},
    ]


@pytest.mark.asyncio
async def test_load_history_for_group_scope_queries_by_group(monkeypatch):
    captured = _StubMessageModel.configure(
        [
            _StubMsg(sender_type="user", content="hi team"),
            _StubMsg(sender_type="agent", content="hello humans"),
        ]
    )
    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _StubMessageModel)

    result = await load_history_for_scope(_group_ctx())

    assert captured["query"] == {
        "context_type": "group",
        "group": "g1",
        "deleted": False,
    }
    # Group rows have no explicit role column — fall back to sender_type.
    assert result == [
        {"role": "user", "content": "hi team"},
        {"role": "assistant", "content": "hello humans"},
    ]


@pytest.mark.asyncio
async def test_load_history_returns_empty_on_mongo_error(monkeypatch):
    class _Boom:
        @classmethod
        def find(cls, q):
            raise RuntimeError("mongo down")

    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _Boom)

    # Mongo blowing up must NOT kill the stream — degrade to no-context reply.
    assert await load_history_for_scope(_session_ctx()) == []


# ---------------------------------------------------------------------------
# Integration test: post_agent_chat ships history on the RunSpec to the executor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_agent_chat_ships_history_on_runspec_to_executor(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — Beanie init for create_run
):
    """POST must rehydrate prior turns and put them on the RunSpec the executor receives."""
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="session"),
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )

    captured_specs: list = []

    class _RecordingExecutor:
        async def submit(self, spec):
            captured_specs.append(spec)

    async def fake_resolver(**_):
        return fake_ctx

    async def fake_persist(_ctx, _body):
        return "user_msg_id_1"

    async def fake_ensure_session(_ctx):
        return None

    prior_history = [
        {"role": "user", "content": "remember my name is Rohit"},
        {"role": "assistant", "content": "got it — hi Rohit"},
    ]

    async def fake_loader(ctx, *, limit=50):  # noqa: ARG001
        assert ctx is fake_ctx
        return list(prior_history)

    # Patch the loader on both modules — the router imported the name at
    # import time, so replacing it on agent_service alone wouldn't affect
    # the router's view.
    with (
        patch.object(router_mod, "resolve_scope_context", fake_resolver),
        patch.object(router_mod, "load_history_for_scope", fake_loader),
        patch.object(agent_service, "load_history_for_scope", fake_loader),
        patch.object(router_mod, "_persist_user_message", fake_persist),
        patch.object(router_mod, "_ensure_scope_session", fake_ensure_session),
        patch.object(router_mod, "get_executor", lambda: _RecordingExecutor()),
        patch.object(router_mod, "get_stream_transport", lambda: _StubTransport()),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "what's my name?", "client_message_id": "c-rehydrate"},
        )

    assert resp.status_code == 200
    assert len(captured_specs) == 1
    assert captured_specs[0].history == prior_history, (
        "The RunSpec handed to the executor must carry rehydrated history so "
        "the agent remembers prior turns across backend restarts / pool evictions."
    )
