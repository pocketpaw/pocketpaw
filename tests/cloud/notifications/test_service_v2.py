"""Tests for the notifications service.

Uses the shared ``mongo_db`` fixture (mongomock-motor) so service
functions exercise real Beanie writes against an isolated in-memory
DB. The ``recording_bus`` autouse fixture captures emitted events;
tests request it explicitly to assert on ``bus.events``.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)
from pocketpaw_ee.cloud.notifications import service as notifications_service

pytestmark = pytest.mark.usefixtures("mongo_db")


async def test_create_persists_and_emits_new(recording_bus) -> None:
    out = await notifications_service.create(
        workspace_id="w1",
        recipient="u2",
        kind="mention",
        title="You were mentioned",
        body="hi",
    )
    assert out.recipient_id == "u2"
    assert out.workspace_id == "w1"
    assert out.kind == "mention"

    fetched = await notifications_service.list_for_user("u2")
    assert len(fetched) == 1
    assert fetched[0].id == out.id

    new_events = [e for e in recording_bus.events if isinstance(e, NotificationNew)]
    assert len(new_events) == 1
    assert new_events[0].data["user_id"] == "u2"
    assert new_events[0].data["workspace_id"] == "w1"
    assert new_events[0].data["kind"] == "mention"
    assert new_events[0].data["read"] is False


async def test_list_for_user_filters_unread() -> None:
    n1 = await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="t1")
    await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="t2")
    await notifications_service.mark_read(n1.id, "u1")

    unread = await notifications_service.list_for_user("u1", unread=True)
    assert len(unread) == 1
    assert unread[0].title == "t2"


async def test_mark_read_emits_event_and_returns_true(recording_bus) -> None:
    n = await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    recording_bus.events.clear()

    changed = await notifications_service.mark_read(n.id, "u1")
    assert changed is True
    read_events = [e for e in recording_bus.events if isinstance(e, NotificationRead)]
    assert len(read_events) == 1
    assert read_events[0].data == {"id": n.id, "user_id": "u1"}


async def test_mark_read_noop_for_wrong_user(recording_bus) -> None:
    n = await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    recording_bus.events.clear()
    changed = await notifications_service.mark_read(n.id, "u_other")
    assert changed is False
    assert recording_bus.events == []


async def test_mark_read_noop_for_already_read(recording_bus) -> None:
    n = await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    await notifications_service.mark_read(n.id, "u1")
    recording_bus.events.clear()
    changed = await notifications_service.mark_read(n.id, "u1")
    assert changed is False
    assert recording_bus.events == []


async def test_clear_all_returns_count_and_emits(recording_bus) -> None:
    await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="a")
    await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="b")
    await notifications_service.create(workspace_id="w1", recipient="u2", kind="m", title="c")
    recording_bus.events.clear()

    count = await notifications_service.clear_all("u1")
    assert count == 2
    cleared = [e for e in recording_bus.events if isinstance(e, NotificationCleared)]
    assert len(cleared) == 1
    assert cleared[0].data == {"user_id": "u1"}


async def test_list_for_user_dicts_returns_legacy_wire_shape() -> None:
    await notifications_service.create(workspace_id="w1", recipient="u1", kind="m", title="t")
    out = await notifications_service.list_for_user_dicts("u1")
    assert isinstance(out, list)
    assert len(out) == 1
    item = out[0]
    assert set(item.keys()) >= {
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
