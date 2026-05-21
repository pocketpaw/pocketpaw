"""Cloud agent chat request and SSE event schema tests."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.agent_schemas import (
    CloudAgentChatRequest,
    SseEventName,
)
from pydantic import ValidationError


def test_request_requires_content():
    with pytest.raises(ValidationError):
        CloudAgentChatRequest(content="")


def test_request_accepts_minimal_body():
    req = CloudAgentChatRequest(content="hello")
    assert req.content == "hello"
    assert req.attachments == []
    assert req.mentions == []
    assert req.reply_to is None
    assert req.agent_id is None
    assert req.client_message_id is None


def test_request_accepts_full_body():
    req = CloudAgentChatRequest(
        content="hi",
        attachments=[{"type": "image", "url": "http://x/y.png"}],
        reply_to="msg_1",
        mentions=[{"type": "agent", "id": "a1"}],
        agent_id="a1",
        client_message_id="client_42",
    )
    assert req.agent_id == "a1"
    assert req.client_message_id == "client_42"


def test_event_names_cover_spec():
    expected = {
        "message.persisted",
        "stream_start",
        "thinking",
        "tool_start",
        "tool_result",
        "chunk",
        "ripple",
        "pocket_created",
        "pocket_mutation",
        "ask_user_question",
        "stream_end",
        "error",
    }
    assert {e.value for e in SseEventName} == expected
