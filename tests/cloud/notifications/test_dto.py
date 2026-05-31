"""Tests for ee.cloud.notifications.dto — wire DTO + mapping."""

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.notifications.domain import Notification, NotificationSource
from pocketpaw_ee.cloud.notifications.dto import NotificationOut, notification_to_dto


def _domain(**overrides) -> Notification:
    base = dict(
        id="n1",
        workspace_id="w1",
        recipient_id="u1",
        kind="mention",
        title="You were mentioned",
        body="hello",
        source=None,
        read=False,
        created_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        expires_at=None,
    )
    base.update(overrides)
    return Notification(**base)


def test_dto_contains_wire_keys() -> None:
    """Asserts the keys the existing wire shape promises:
    id, user_id, workspace_id, kind, title, body, source_id, read, created_at."""
    out = NotificationOut(
        id="n1",
        user_id="u1",
        workspace_id="w1",
        kind="mention",
        title="t",
        body="b",
        source_id=None,
        read=False,
        created_at="2026-04-27T12:00:00+00:00",
    )
    dump = out.model_dump()
    assert set(dump.keys()) == {
        "id",
        "user_id",
        "workspace_id",
        "kind",
        "title",
        "body",
        "source_id",
        "read",
        "created_at",
    }


def test_notification_to_dto_no_source() -> None:
    out = notification_to_dto(_domain())
    assert out.id == "n1"
    assert out.user_id == "u1"
    assert out.workspace_id == "w1"
    assert out.kind == "mention"
    assert out.title == "You were mentioned"
    assert out.body == "hello"
    assert out.source_id is None
    assert out.read is False
    assert out.created_at == "2026-04-27T12:00:00+00:00"


def test_notification_to_dto_with_source() -> None:
    src = NotificationSource(type="message", id="m42", pocket_id=None)
    out = notification_to_dto(_domain(source=src))
    assert out.source_id == "m42"


def test_notification_to_dto_serializes_naive_created_at_as_utc() -> None:
    """Beanie reads return naive datetimes; iso_utc anchors them to +00:00."""
    naive = datetime(2026, 4, 27, 12, 0, 0)
    out = notification_to_dto(_domain(created_at=naive))
    assert out.created_at == "2026-04-27T12:00:00+00:00"
