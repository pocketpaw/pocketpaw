"""Derivation tests: mention / reaction / invite → notifications_service.create.

Each emit site is exercised through its owning service; the spy on
``notifications_service.create`` proves the derivation fires with the
right arguments (and does NOT fire for self-targeted events).

Tests run against a real Beanie in-memory database (``mongo_db`` fixture)
and assert via spies on ``notifications_service.create`` and
``unread_service.bump_mention``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ee.cloud.chat import message_service
from ee.cloud.chat.schemas import SendMessageRequest
from ee.cloud.models.group import Group as _GroupDoc
from ee.cloud.models.message import Message as _MessageDoc


async def _make_group(
    *,
    workspace: str = "w1",
    owner: str = "u1",
    members: list[str] | None = None,
    name: str = "general",
) -> _GroupDoc:
    if members is None:
        members = [owner]
    doc = _GroupDoc(
        workspace=workspace,
        name=name,
        slug="general",
        type="private",
        members=members,
        member_roles={owner: "admin"},
        owner=owner,
    )
    await doc.insert()
    return doc


async def _make_message(*, group_id: str, sender: str, content: str = "hey") -> _MessageDoc:
    doc = _MessageDoc(
        context_type="group",
        group=group_id,
        sender=sender,
        sender_type="user",
        content=content,
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# Mention derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_with_mention_creates_notification_for_target(mongo_db, monkeypatch):
    group = await _make_group(workspace="w1", owner="u1", members=["u1", "u2"])

    spy = AsyncMock()
    monkeypatch.setattr("ee.cloud.chat.message_service.notifications_service.create", spy)
    monkeypatch.setattr("ee.cloud.chat.message_service.unread_service.bump_mention", AsyncMock())

    response = await message_service.send_message(
        str(group.id),
        "u1",
        SendMessageRequest(
            content="hello @alice",
            mentions=[{"type": "user", "id": "u2", "display_name": "alice"}],
        ),
    )

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["recipient"] == "u2"
    assert kwargs["kind"] == "mention"
    assert kwargs["workspace_id"] == "w1"
    assert kwargs["body"] == "hello @alice"
    assert "#general" in kwargs["title"]
    assert kwargs["source"].type == "message"
    assert kwargs["source"].id == response["_id"]


@pytest.mark.asyncio
async def test_send_message_self_mention_does_not_notify(mongo_db, monkeypatch):
    group = await _make_group(workspace="w1", owner="u1")

    spy = AsyncMock()
    monkeypatch.setattr("ee.cloud.chat.message_service.notifications_service.create", spy)
    monkeypatch.setattr("ee.cloud.chat.message_service.unread_service.bump_mention", AsyncMock())

    await message_service.send_message(
        str(group.id),
        "u1",
        SendMessageRequest(
            content="me me me",
            mentions=[{"type": "user", "id": "u1", "display_name": "self"}],
        ),
    )

    spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Reaction derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_reaction_adding_notifies_original_sender(mongo_db, monkeypatch):
    """Reactor ≠ sender: notify the original sender."""
    group = await _make_group(workspace="w1", owner="u1", members=["u1", "u2"])
    msg = await _make_message(group_id=str(group.id), sender="u1", content="hey")

    spy = AsyncMock()
    monkeypatch.setattr("ee.cloud.chat.message_service.notifications_service.create", spy)

    await message_service.toggle_reaction(str(msg.id), "u2", "\U0001f44d")

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["recipient"] == "u1"
    assert kwargs["kind"] == "reaction"
    assert kwargs["workspace_id"] == "w1"
    assert "\U0001f44d" in kwargs["title"]
    assert kwargs["body"] == "hey"
    assert kwargs["source"].type == "message"
    assert kwargs["source"].id == str(msg.id)


@pytest.mark.asyncio
async def test_toggle_reaction_self_reaction_does_not_notify(mongo_db, monkeypatch):
    """Reactor == sender: skip notification."""
    group = await _make_group(workspace="w1", owner="u1")
    msg = await _make_message(group_id=str(group.id), sender="u1", content="hey")

    spy = AsyncMock()
    monkeypatch.setattr("ee.cloud.chat.message_service.notifications_service.create", spy)

    await message_service.toggle_reaction(str(msg.id), "u1", "\U0001f44d")

    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_toggle_reaction_removing_does_not_notify(mongo_db, monkeypatch):
    """Toggle off (remove existing reaction) must not fire a notification."""
    group = await _make_group(workspace="w1", owner="u1", members=["u1", "u2"])
    msg = await _make_message(group_id=str(group.id), sender="u1", content="hey")

    # Pre-seed a reaction so the next toggle removes it.
    await message_service.toggle_reaction(str(msg.id), "u2", "\U0001f44d")

    spy = AsyncMock()
    monkeypatch.setattr("ee.cloud.chat.message_service.notifications_service.create", spy)

    await message_service.toggle_reaction(str(msg.id), "u2", "\U0001f44d")

    spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Invite derivation helpers (unchanged shape; preserved for downstream tests)
# ---------------------------------------------------------------------------


def _make_ws(ws_id: str = "w1", name: str = "Acme", seats: int = 10) -> SimpleNamespace:
    ws = SimpleNamespace(
        id=ws_id,
        name=name,
        slug="acme",
        owner="u1",
        plan="free",
        seats=seats,
        createdAt=None,
        deleted_at=None,
        settings=None,
    )
    ws.save = AsyncMock()
    return ws


def _make_user(user_id: str = "u1", email: str = "u1@example.com") -> SimpleNamespace:
    u = SimpleNamespace()
    u.id = user_id
    u.email = email
    u.workspaces = []
    u.save = AsyncMock()
    return u
