"""Tests for ee.cloud.notifications.domain — pure value objects."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.notifications.domain import Notification, NotificationSource


def test_notification_source_is_frozen() -> None:
    src = NotificationSource(type="message", id="m1", pocket_id=None)
    with pytest.raises(FrozenInstanceError):
        src.type = "comment"  # type: ignore[misc]


def test_notification_source_with_pocket_id() -> None:
    src = NotificationSource(type="comment", id="c1", pocket_id="p1")
    assert src.pocket_id == "p1"


def test_notification_construct_minimal() -> None:
    n = Notification(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="t",
        body="",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        expires_at=None,
    )
    assert n.id == "n1"
    assert n.read is False
    assert n.source is None
    assert n.expires_at is None


def test_notification_with_source() -> None:
    src = NotificationSource(type="message", id="m1", pocket_id=None)
    n = Notification(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="t",
        body="b",
        source=src,
        read=False,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )
    assert n.source is src


def test_notification_is_frozen() -> None:
    n = Notification(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="t",
        body="",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        n.read = True  # type: ignore[misc]
