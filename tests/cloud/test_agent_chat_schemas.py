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
        intent="skill:summarize",
        skill_args="last 7 days",
    )
    assert req.agent_id == "a1"
    assert req.client_message_id == "client_42"
    assert req.intent == "skill:summarize"
    assert req.skill_args == "last 7 days"


def test_intent_defaults_to_none():
    req = CloudAgentChatRequest(content="hello")
    assert req.intent is None
    assert req.skill_args is None


@pytest.mark.parametrize("value", ["pocket_create", "skill:foo", "skill:", None])
def test_intent_accepts_known_values(value):
    """``pocket_create``, any ``skill:<name>``, and null are valid."""
    req = CloudAgentChatRequest(content="hi", intent=value)
    assert req.intent == value


@pytest.mark.parametrize("value", ["pocket-create", "Pocket_Create", "skill", "", "create"])
def test_intent_rejects_unknown_values(value):
    """A typo of a known intent must 422, not be silently ignored."""
    with pytest.raises(ValidationError):
        CloudAgentChatRequest(content="hi", intent=value)


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
