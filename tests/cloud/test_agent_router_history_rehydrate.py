"""Regression: backend restart / pool eviction must not wipe agent memory.

The agent backends keep in-process conversation state keyed by ``session_key``.
That state doesn't survive a backend restart or an ``AgentPool`` LRU eviction,
so the router must rehydrate history from the persisted ``Message`` collection
before calling ``pool.run``. Before this fix, ``history=None`` was passed
unconditionally and the agent replied with no memory of prior turns after any
process restart.
"""

from __future__ import annotations

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
# Integration test: post_agent_chat forwards history to pool.run
# ---------------------------------------------------------------------------


class _RecordingPool:
    """Captures the ``history`` kwarg that ``pool.run`` receives."""

    def __init__(self):
        self.history_seen: list[dict[str, str]] | None = None

    async def get(self, agent_id):
        return SimpleNamespace(agent_id=agent_id, agent_name="Agent " + agent_id)

    async def run(self, agent_id, message, session_key, history=None, knowledge_context=""):
        self.history_seen = history
        yield SimpleNamespace(type="message", content="ok", metadata={})
        yield SimpleNamespace(type="done", content="", metadata={})

    async def observe(self, agent_id, user_input, agent_output):
        return None


@pytest.mark.asyncio
async def test_post_agent_chat_rehydrates_history_into_pool_run(
    cloud_app_client: AsyncClient,
):
    fake_ctx = _session_ctx()
    fake_pool = _RecordingPool()

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id_1"

    async def fake_ensure_session(ctx):
        return None

    class _FakeMsg:
        id = "assistant_msg_id_1"
        createdAt = __import__("datetime").datetime.now(__import__("datetime").UTC)

    async def fake_persist_assistant(ctx, content, attachments):
        return _FakeMsg()

    async def fake_broadcast_new(ctx, message_id, content, attachments, created_at):
        return None

    async def fake_broadcast_typing(ctx, active):
        return None

    prior_history = [
        {"role": "user", "content": "remember my name is Rohit"},
        {"role": "assistant", "content": "got it — hi Rohit"},
    ]

    async def fake_loader(ctx, *, limit=50):
        assert ctx is fake_ctx
        return list(prior_history)

    # Patch on both modules — the router imported the name at import time, so
    # replacing it on agent_service alone wouldn't affect the router's view.
    with (
        patch.object(router_mod, "resolve_scope_context", fake_resolver),
        patch.object(router_mod, "load_history_for_scope", fake_loader),
        patch.object(agent_service, "load_history_for_scope", fake_loader),
        patch.object(router_mod, "_persist_user_message", fake_persist),
        patch.object(router_mod, "_ensure_scope_session", fake_ensure_session),
        patch.object(router_mod, "_persist_assistant_message", fake_persist_assistant),
        patch.object(router_mod, "_broadcast_message_new", fake_broadcast_new),
        patch.object(router_mod, "_broadcast_agent_typing", fake_broadcast_typing),
        patch.object(router_mod, "get_agent_pool", lambda: fake_pool),
    ):
        async with cloud_app_client.stream(
            "POST",
            "/cloud/chat/session/s1/agent",
            json={"content": "what's my name?"},
        ) as resp:
            assert resp.status_code == 200
            # Drain the stream so the generator finishes (and observe() is called).
            await resp.aread()

    assert fake_pool.history_seen == prior_history, (
        "pool.run must receive rehydrated history so the agent remembers "
        "prior turns across backend restarts / pool evictions."
    )
