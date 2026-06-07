"""TDD: session-scope message persistence in _persist_user_message / _persist_assistant_message."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.agent_router import _persist_assistant_message, _persist_user_message
from pocketpaw_ee.cloud.chat.agent_schemas import CloudAgentChatRequest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind


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


@pytest.mark.asyncio
async def test_persist_user_message_session_scope(monkeypatch):
    captured = {}

    class _StubMessage:
        def __init__(self, **kw):
            captured.update(kw)
            self.id = "mid-1"

        async def insert(self):
            return None

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.message_service._MessageDoc", _StubMessage)

    body = CloudAgentChatRequest(content="hello session")
    mid = await _persist_user_message(_session_ctx(), body)

    assert mid == "mid-1"
    assert captured["context_type"] == "session"
    assert captured["session_key"] == "cloud:session:s1:a1"
    assert captured["role"] == "user"
    assert captured["content"] == "hello session"
    assert captured["sender"] == "u1"
    assert captured["sender_type"] == "user"
    assert captured["workspace_id"] == "w1"
    assert "group" not in captured or captured.get("group") is None


@pytest.mark.asyncio
async def test_persist_assistant_message_session_scope(monkeypatch):
    captured = {}

    class _StubMessage:
        def __init__(self, **kw):
            captured.update(kw)
            self.id = "mid-2"

        async def insert(self):
            return None

    class _StubAttachment:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.message_service._MessageDoc", _StubMessage)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.message_service._AttachmentDoc", _StubAttachment)

    await _persist_assistant_message(_session_ctx(), "hi back", [])

    assert captured["context_type"] == "session"
    assert captured["session_key"] == "cloud:session:s1:a1"
    assert captured["role"] == "assistant"
    assert captured["content"] == "hi back"
    assert captured["sender_type"] == "agent"
    assert captured["agent"] == "a1"
    assert captured["workspace_id"] == "w1"


def test_message_model_accepts_session_context_type():
    """Validator must accept context_type=session with pocket-shape fields."""
    from pocketpaw_ee.cloud.models.message import Message

    # Use model_construct to bypass Beanie's DB-requiring __init__, then run
    # the validator manually — same pattern as tests/cloud/test_models.py.
    msg = Message.model_construct(
        context_type="session",
        session_key="cloud:session:s1:a1",
        role="user",
        sender="u1",
        sender_type="user",
        content="hi",
        workspace_id="w1",
    )
    validated = msg._enforce_context()
    assert validated.context_type == "session"
    assert validated.session_key == "cloud:session:s1:a1"


def test_message_model_session_rejects_group_field():
    """Validator must reject session messages that carry a group field."""
    from pocketpaw_ee.cloud.models.message import Message

    msg = Message.model_construct(
        context_type="session",
        session_key="k",
        role="user",
        group="g1",  # session must not have group
        content="x",
    )
    with pytest.raises(ValueError):
        msg._enforce_context()
