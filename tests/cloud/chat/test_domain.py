"""Tests for ee.cloud.chat.domain — pure value objects."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.chat.domain import (
    Attachment,
    Group,
    GroupAgent,
    Mention,
    Message,
    Reaction,
)


def test_message_is_frozen() -> None:
    m = Message(
        id="m1",
        context_type="group",
        workspace_id="w1",
        group="g1",
        sender="u1",
        sender_type="user",
        agent=None,
        content="hi",
    )
    with pytest.raises(FrozenInstanceError):
        m.content = "x"  # type: ignore[misc]


def test_message_minimal_fields() -> None:
    m = Message(
        id="m1",
        context_type="group",
        workspace_id="w1",
        group="g1",
        sender="u1",
        sender_type="user",
        agent=None,
        content="hi",
    )
    assert m.thread_count == 0
    assert m.deleted is False
    assert m.mentions == ()
    assert m.reactions == ()


def test_message_with_mentions_and_reactions() -> None:
    mention = Mention(type="user", id="u2", display_name="Bob")
    reaction = Reaction(emoji="👍", users=("u1", "u2"))
    m = Message(
        id="m1",
        context_type="group",
        workspace_id="w1",
        group="g1",
        sender="u1",
        sender_type="user",
        agent=None,
        content="hi @bob",
        mentions=(mention,),
        reactions=(reaction,),
    )
    assert m.mentions[0].id == "u2"
    assert m.reactions[0].emoji == "👍"


def test_pocket_message_has_session_key_and_role() -> None:
    m = Message(
        id="m1",
        context_type="pocket",
        workspace_id="w1",
        group=None,
        sender=None,
        sender_type="user",
        agent=None,
        content="hello",
        session_key="cloud:pocket:p1:agent1",
        role="user",
    )
    assert m.session_key == "cloud:pocket:p1:agent1"
    assert m.role == "user"


def test_group_is_frozen() -> None:
    g = Group(
        id="g1",
        workspace_id="w1",
        name="general",
        slug="general",
        description="",
        icon="",
        color="",
        type="public",
        members=("u1",),
        member_roles=(),
        agents=(),
        pinned_messages=(),
        owner="u1",
        archived=False,
        last_message_at=None,
        message_count=0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        g.name = "x"  # type: ignore[misc]


def test_group_with_agents() -> None:
    agent = GroupAgent(agent_id="a1", role="assistant", respond_mode="auto")
    g = Group(
        id="g1",
        workspace_id="w1",
        name="general",
        slug="general",
        description="",
        icon="",
        color="",
        type="public",
        members=("u1",),
        member_roles=(("u1", "admin"),),
        agents=(agent,),
        pinned_messages=(),
        owner="u1",
        archived=False,
        last_message_at=None,
        message_count=0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert g.agents[0].agent_id == "a1"
    assert g.agents[0].respond_mode == "auto"


def test_attachment_with_meta() -> None:
    a = Attachment(
        type="file",
        url="https://...",
        name="doc.pdf",
        meta=(("size", 1024), ("mime", "application/pdf")),
    )
    assert a.url == "https://..."
    assert a.meta[0] == ("size", 1024)


def test_value_object_equality() -> None:
    m1 = Mention(type="user", id="u1", display_name="A")
    m2 = Mention(type="user", id="u1", display_name="A")
    assert m1 == m2
    assert hash(m1) == hash(m2)
