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


def test_model_defaults_to_none():
    """CS-13 — no model picker means no override; the backend picks the model."""
    req = CloudAgentChatRequest(content="hi")
    assert req.model is None


@pytest.mark.parametrize(
    "value",
    [
        "claude-haiku-4-5-20251001",
        "claude-opus-4-8",
        "anthropic:claude-sonnet-4-6",
        "anthropic/claude-sonnet-4-6",
        "gpt-5.2",
        "ollama:llama3.2",
    ],
)
def test_model_accepts_valid_ids(value):
    """CS-13 — real model ids (letters, digits, ``. _ : / -``) validate."""
    req = CloudAgentChatRequest(content="hi", model=value)
    assert req.model == value


@pytest.mark.parametrize(
    "value",
    [
        "claude haiku",  # whitespace
        "claude;rm -rf /",  # shell metacharacters
        "model$(whoami)",  # command substitution
        "model|cat",  # pipe
        "",  # empty string
        "m" * 101,  # over max_length
    ],
)
def test_model_rejects_hostile_or_malformed_ids(value):
    """CS-13 — the field lands in a subprocess launch arg, so whitespace, shell
    metacharacters, the empty string, and >100 chars must 422 at the edge."""
    with pytest.raises(ValidationError):
        CloudAgentChatRequest(content="hi", model=value)


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
        "pocket_execution",
        "ask_user_question",
        "token_usage",
        "stream_end",
        "error",
    }
    assert {e.value for e in SseEventName} == expected
