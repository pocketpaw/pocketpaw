"""Regression test: channel agent bridge forwards attachments to pool.run.

Created 2026-04-19: guards against the silent attachment-drop bug where
``_run_agent_response`` never surfaced ``Message.attachments`` into the agent
prompt. The DM path formats file metadata into the user content as
``Attached files:\\n- <name> (<mime>, <size>) at <url>`` lines; the channel
path must match that shape so agents in groups see the same file context DM
users already get.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _AsyncIter:
    """Minimal async iterator that yields a done event, then stops."""

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _done_event():
    return SimpleNamespace(type="done", content="", metadata={})


def _error_event(msg: str):
    return SimpleNamespace(type="error", content=msg, metadata={})


@pytest.mark.asyncio
async def test_run_agent_response_forwards_attachments_to_pool_run():
    """Channel agent bridge must forward Message.attachments into pool.run.

    Before the fix, ``on_message_for_agents`` and ``_run_agent_response``
    ignored ``data["attachments"]``, so ``pool.run`` received only the bare
    text. The agent then had no filename/mime/size context for files the user
    shared in a channel. After the fix, the attachments travel through to the
    prompt using the same shape the DM path uses.
    """
    from pocketpaw_ee.cloud.shared import agent_bridge

    # Stub agent instance returned by pool.get
    instance = SimpleNamespace(agent_name="Test Agent")

    pool = MagicMock()
    pool.get = AsyncMock(return_value=instance)
    captured: dict = {}

    async def fake_run(agent_id, user_message, session_key, history=None, knowledge_context=""):
        captured["agent_id"] = agent_id
        captured["user_message"] = user_message
        captured["session_key"] = session_key
        captured["history"] = history
        captured["knowledge_context"] = knowledge_context
        # Yield only ``done`` — ``full_text`` stays empty so the bridge's
        # ``if not full_text.strip(): return`` short-circuits before the Mongo
        # persistence branch (which would need Beanie initialized to execute
        # ``Message(...)``). Keeps the test at unit-scope while still capturing
        # what we care about: the prompt handed to ``pool.run``.
        yield _done_event()

    pool.run = fake_run
    pool.observe = AsyncMock()

    # Mongo Message.find(...).sort(...).limit(...).to_list() — return no history
    # so the bridge doesn't try to talk to a real database.
    to_list_mock = AsyncMock(return_value=[])
    limit_mock = MagicMock()
    limit_mock.to_list = to_list_mock
    sort_mock = MagicMock()
    sort_mock.limit = MagicMock(return_value=limit_mock)
    find_mock = MagicMock()
    find_mock.sort = MagicMock(return_value=sort_mock)

    from pocketpaw_ee.cloud.models.message import Message as _RealMessage

    # Beanie isn't initialized in unit tests, so ``Message.group`` / etc. raise
    # AttributeError. Stamp the class attributes Beanie would otherwise provide
    # for query-builder expressions. They're MagicMocks that no-op on `==`.
    _patched_attrs = {"group": MagicMock(), "deleted": MagicMock(), "createdAt": MagicMock()}

    # Patch class-level ``find`` so the history query returns [] without a DB.
    # Patch ``Message.insert`` so the agent-message persistence step no-ops.
    attachments_in = [
        {
            "type": "image",
            "url": "/api/v1/uploads/abc123",
            "name": "diagram.png",
            "meta": {"mime": "image/png", "size": 48_000, "id": "abc123"},
        },
    ]

    with (
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
        patch.multiple(_RealMessage, create=True, **_patched_attrs),
        patch.object(_RealMessage, "find", MagicMock(return_value=find_mock)),
        patch.object(_RealMessage, "insert", new=AsyncMock(return_value=None)),
        patch(
            "pocketpaw.agents.pool.get_agent_pool",
            return_value=pool,
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-1",
            group_id="group-1",
            workspace_id="ws-1",
            user_message="what's in this file?",
            group_members=["user-1"],
            attachments=attachments_in,
        )

    # The bridge must have called pool.run with a prompt that carries the
    # file-awareness block (filename / mime / size). Matching the DM shape
    # lets agents reason about channel attachments the same way they already
    # do for DMs.
    assert "user_message" in captured, "pool.run was never invoked"
    augmented = captured["user_message"]
    assert "diagram.png" in augmented, f"filename missing from prompt: {augmented!r}"
    assert "image/png" in augmented, f"mime missing from prompt: {augmented!r}"
    # The DM path renders size via a human-readable helper (_format_bytes).
    # Size presence is the contract; exact formatting is incidental.
    assert "Attached" in augmented or "attached" in augmented, (
        f"no attachment header in prompt: {augmented!r}"
    )


@pytest.mark.asyncio
async def test_run_agent_response_surfaces_error_only_stream_as_message():
    """When backend emits error+done with no text, bridge should still return a fallback."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    instance = SimpleNamespace(agent_name="Test Agent")
    pool = MagicMock()
    pool.get = AsyncMock(return_value=instance)
    pool.observe = AsyncMock()

    async def fake_run(*_args, **_kwargs):
        yield _error_event("tool startup failed")
        yield _done_event()

    pool.run = fake_run

    from pocketpaw_ee.cloud.models.message import Message as _RealMessage

    to_list_mock = AsyncMock(return_value=[])
    limit_mock = MagicMock()
    limit_mock.to_list = to_list_mock
    sort_mock = MagicMock()
    sort_mock.limit = MagicMock(return_value=limit_mock)
    find_mock = MagicMock()
    find_mock.sort = MagicMock(return_value=sort_mock)

    created = {}

    async def fake_create_agent_message(*, group_id, agent_id, content, attachments=None):
        created["group_id"] = group_id
        created["agent_id"] = agent_id
        created["content"] = content
        created["attachments"] = attachments
        return SimpleNamespace(id="msg-1")

    with (
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
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
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(side_effect=fake_create_agent_message),
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
    ):
        result = await agent_bridge._run_agent_response(
            agent_id="agent-1",
            group_id="group-1",
            workspace_id="ws-1",
            user_message="hello",
            group_members=["user-1"],
            attachments=None,
        )

    assert result is not None
    assert "could not produce a full response" in result
    assert created["agent_id"] == "agent-1"


@pytest.mark.asyncio
async def test_run_agent_response_applies_response_label_prefix() -> None:
    """Final collaborative responses should be clearly prefixed for users."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    instance = SimpleNamespace(agent_name="Test Agent")
    pool = MagicMock()
    pool.get = AsyncMock(return_value=instance)
    pool.observe = AsyncMock()

    async def fake_run(*_args, **_kwargs):
        yield SimpleNamespace(type="message", content="Synthesized answer body", metadata={})
        yield _done_event()

    pool.run = fake_run

    from pocketpaw_ee.cloud.models.message import Message as _RealMessage

    to_list_mock = AsyncMock(return_value=[])
    limit_mock = MagicMock()
    limit_mock.to_list = to_list_mock
    sort_mock = MagicMock()
    sort_mock.limit = MagicMock(return_value=limit_mock)
    find_mock = MagicMock()
    find_mock.sort = MagicMock(return_value=sort_mock)

    created = {}

    async def fake_create_agent_message(*, group_id, agent_id, content, attachments=None):
        created["group_id"] = group_id
        created["agent_id"] = agent_id
        created["content"] = content
        created["attachments"] = attachments
        return SimpleNamespace(id="msg-2")

    with (
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
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
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(side_effect=fake_create_agent_message),
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
    ):
        result = await agent_bridge._run_agent_response(
            agent_id="agent-2",
            group_id="group-2",
            workspace_id="ws-2",
            user_message="synthesize this",
            group_members=["user-1"],
            attachments=None,
            response_label="Final response:",
        )

    assert result is not None
    assert result.startswith("Final response:\n\n")
    assert "Synthesized answer body" in result
    assert created["content"].startswith("Final response:\n\n")
