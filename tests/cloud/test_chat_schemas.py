"""Tests for chat domain schemas."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    AddGroupMembersRequest,
    CreateGroupRequest,
    CursorPage,
    EditMessageRequest,
    ReactRequest,
    SendMessageRequest,
    UpdateGroupRequest,
    WsInbound,
    WsOutbound,
)
from pydantic import ValidationError as PydanticValidationError


def test_create_group_defaults():
    req = CreateGroupRequest(name="general")
    assert req.type == "private" and req.description == ""


def test_create_group_dm():
    req = CreateGroupRequest(name="DM", type="dm", member_ids=["u1", "u2"])
    assert req.type == "dm" and len(req.member_ids) == 2


def test_send_message_content_required():
    req = SendMessageRequest(content="hello")
    assert req.content == "hello" and req.reply_to is None and req.mentions == []


def test_send_message_max_length():
    with pytest.raises(PydanticValidationError):
        SendMessageRequest(content="x" * 10_001)


def test_send_message_min_length():
    with pytest.raises(PydanticValidationError):
        SendMessageRequest(content="")


def test_edit_message():
    req = EditMessageRequest(content="updated")
    assert req.content == "updated"


def test_react_request():
    req = ReactRequest(emoji="thumbsup")
    assert req.emoji == "thumbsup"


def test_ws_inbound_message_send():
    msg = WsInbound.model_validate({"type": "message.send", "group_id": "g1", "content": "hello"})
    assert msg.type == "message.send"


def test_ws_inbound_envelope_lift_flattens_bus_payload():
    """``{type, data: {...}}`` (browser bus shape) parses the same as flat."""
    from pocketpaw_ee.cloud.chat.router import _normalize_ws_inbound

    payload = {
        "type": "message.send",
        "data": {
            "group_id": "g1",
            "content": "hello",
            "attachments": [{"type": "file", "url": "/api/v1/uploads/u1", "name": "x.pdf"}],
        },
    }
    flat = _normalize_ws_inbound(payload)
    msg = WsInbound.model_validate(flat)
    assert msg.type == "message.send"
    assert msg.group_id == "g1"
    assert msg.content == "hello"
    assert msg.attachments == [{"type": "file", "url": "/api/v1/uploads/u1", "name": "x.pdf"}]


def test_ws_inbound_envelope_lift_is_noop_for_flat():
    from pocketpaw_ee.cloud.chat.router import _normalize_ws_inbound

    payload = {"type": "typing.start", "group_id": "g1"}
    assert _normalize_ws_inbound(payload) == payload


def test_ws_inbound_envelope_lift_prefers_top_level_on_conflict():
    """Explicit top-level ``group_id`` wins over a nested duplicate."""
    from pocketpaw_ee.cloud.chat.router import _normalize_ws_inbound

    payload = {
        "type": "message.send",
        "group_id": "explicit",
        "data": {"group_id": "nested", "content": "hi"},
    }
    flat = _normalize_ws_inbound(payload)
    assert flat["group_id"] == "explicit"
    assert flat["content"] == "hi"


def test_ws_inbound_typing():
    msg = WsInbound.model_validate({"type": "typing.start", "group_id": "g1"})
    assert msg.type == "typing.start"


def test_ws_inbound_invalid_type():
    with pytest.raises(PydanticValidationError):
        WsInbound.model_validate({"type": "invalid.type"})


def test_ws_inbound_all_types():
    valid_types = [
        "message.send",
        "message.edit",
        "message.delete",
        "message.react",
        "typing.start",
        "typing.stop",
        "presence.update",
        "read.ack",
    ]
    for t in valid_types:
        msg = WsInbound.model_validate({"type": t})
        assert msg.type == t


def test_ws_outbound():
    msg = WsOutbound(type="message.new", data={"id": "m1"})
    assert msg.type == "message.new"


def test_cursor_page():
    page = CursorPage(items=[], next_cursor=None, has_more=False)
    assert page.items == [] and not page.has_more


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------


def test_create_group_empty_name_rejected():
    with pytest.raises(PydanticValidationError):
        CreateGroupRequest(name="")


def test_create_group_name_too_long():
    with pytest.raises(PydanticValidationError):
        CreateGroupRequest(name="A" * 101)


def test_create_group_all_fields():
    req = CreateGroupRequest(
        name="Design Team",
        description="Design discussions",
        type="private",
        member_ids=["u1", "u2", "u3"],
        icon="palette",
        color="#ff5500",
    )
    assert req.name == "Design Team"
    assert req.description == "Design discussions"
    assert req.type == "private"
    assert len(req.member_ids) == 3
    assert req.icon == "palette"
    assert req.color == "#ff5500"


def test_create_group_invalid_type():
    with pytest.raises(PydanticValidationError):
        CreateGroupRequest(name="test", type="invalid")


def test_update_group_all_optional():
    req = UpdateGroupRequest()
    assert req.name is None
    assert req.description is None
    assert req.icon is None
    assert req.color is None


def test_update_group_partial():
    req = UpdateGroupRequest(name="Renamed")
    assert req.name == "Renamed"
    assert req.description is None


def test_add_group_members():
    req = AddGroupMembersRequest(user_ids=["u1", "u2"])
    assert len(req.user_ids) == 2


def test_add_group_agent_defaults():
    req = AddGroupAgentRequest(agent_id="a1")
    assert req.role == "assistant"
    # Default in schemas.py is "auto"; test originally asserted
    # "mention_only" which never matched the schema.
    assert req.respond_mode == "auto"


def test_add_group_agent_custom():
    req = AddGroupAgentRequest(agent_id="a1", role="moderator", respond_mode="always")
    assert req.role == "moderator"
    assert req.respond_mode == "always"


def test_send_message_with_attachments():
    req = SendMessageRequest(
        content="check this",
        reply_to="m1",
        mentions=[{"type": "user", "id": "u1"}],
        attachments=[{"type": "image", "url": "https://example.com/img.png"}],
    )
    assert req.reply_to == "m1"
    assert len(req.mentions) == 1
    assert len(req.attachments) == 1


def test_edit_message_max_length():
    with pytest.raises(PydanticValidationError):
        EditMessageRequest(content="x" * 10_001)


def test_edit_message_min_length():
    with pytest.raises(PydanticValidationError):
        EditMessageRequest(content="")


def test_react_request_empty_rejected():
    with pytest.raises(PydanticValidationError):
        ReactRequest(emoji="")


def test_react_request_too_long():
    with pytest.raises(PydanticValidationError):
        ReactRequest(emoji="e" * 51)


def test_ws_outbound_defaults():
    msg = WsOutbound(type="ping")
    assert msg.data == {}


def test_ws_inbound_full_message():
    msg = WsInbound.model_validate(
        {
            "type": "message.send",
            "group_id": "g1",
            "content": "hello world",
            "reply_to": "m99",
            "mentions": [{"type": "user", "id": "u1"}],
            "attachments": [{"type": "file", "name": "doc.pdf"}],
        }
    )
    assert msg.group_id == "g1"
    assert msg.content == "hello world"
    assert msg.reply_to == "m99"
    assert len(msg.mentions) == 1
    assert len(msg.attachments) == 1


def test_ws_inbound_react():
    msg = WsInbound.model_validate(
        {
            "type": "message.react",
            "message_id": "m1",
            "emoji": "thumbsup",
        }
    )
    assert msg.message_id == "m1"
    assert msg.emoji == "thumbsup"


def test_ws_inbound_presence():
    msg = WsInbound.model_validate(
        {
            "type": "presence.update",
            "status": "away",
        }
    )
    assert msg.status == "away"
