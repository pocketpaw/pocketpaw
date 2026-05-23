"""Tests for meeting bridges — notifications + calendar.

Notifications: meeting.* events on shared.events.event_bus → in-app
notifications. Calendar: calendar.event.created → auto-create a Meeting
when the event description has a Zoom/Meet URL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.meetings.bridges import calendar as calendar_bridge
from pocketpaw_ee.cloud.meetings.bridges import notifications as notif_bridge
from pocketpaw_ee.cloud.shared.events import EventBus

# ---------------------------------------------------------------------------
# URL detection — pure function, no DB
# ---------------------------------------------------------------------------


def test_detect_zoom_url():
    text = "Join the standup at https://us02web.zoom.us/j/87654321?pwd=abc"
    assert calendar_bridge.detect_meeting_url(text) == (
        "zoom",
        "https://us02web.zoom.us/j/87654321?pwd=abc",
    )


def test_detect_google_meet_url():
    text = "Conf: https://meet.google.com/abc-defg-hij — see you there"
    assert calendar_bridge.detect_meeting_url(text) == (
        "google_meet",
        "https://meet.google.com/abc-defg-hij",
    )


def test_detect_returns_none_for_no_url():
    assert calendar_bridge.detect_meeting_url("just a normal description") is None
    assert calendar_bridge.detect_meeting_url("") is None


def test_detect_picks_first_known_provider():
    """First match wins — Zoom is listed first, so it beats Meet in the
    rare event both appear in one description."""
    text = "primary https://zoom.us/j/1 backup https://meet.google.com/xyz-abc-def"
    provider, url = calendar_bridge.detect_meeting_url(text)
    assert provider == "zoom"
    assert "zoom.us" in url


# ---------------------------------------------------------------------------
# Notification bridge — handlers call notifications_service.create
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_notifications(monkeypatch):
    """Replace notifications_service.create with an AsyncMock so we can
    assert on calls without touching Mongo."""
    fake = AsyncMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.notifications.service.create", fake)
    return fake


async def test_meeting_scheduled_notification_fired(patched_notifications):
    await notif_bridge._on_meeting_scheduled(
        {
            "workspace_id": "ws-1",
            "meeting_id": "m-1",
            "created_by": "user-A",
            "provider": "zoom",
        }
    )
    patched_notifications.assert_called_once()
    kw = patched_notifications.call_args.kwargs
    assert kw["workspace_id"] == "ws-1"
    assert kw["recipient"] == "user-A"
    assert kw["kind"] == "meeting_scheduled"
    assert kw["title"] == "Meeting scheduled"
    assert "Zoom" in kw["body"]
    assert kw["source"].type == "meeting"
    assert kw["source"].id == "m-1"


async def test_meeting_cancelled_notification_fired(patched_notifications):
    await notif_bridge._on_meeting_cancelled(
        {
            "workspace_id": "ws-1",
            "meeting_id": "m-1",
            "cancelled_by_user_id": "user-B",
        }
    )
    patched_notifications.assert_called_once()
    assert patched_notifications.call_args.kwargs["kind"] == "meeting_cancelled"


async def test_notification_handler_skips_when_recipient_missing(patched_notifications):
    """An event without a known recipient should silently no-op rather
    than crash the bus dispatcher (which would block sibling handlers)."""
    await notif_bridge._on_meeting_scheduled(
        {"workspace_id": "ws-1", "meeting_id": "m-1"}  # no created_by
    )
    patched_notifications.assert_not_called()


def test_register_meeting_notification_listeners_idempotent():
    """Registering twice doesn't fork the bus (handlers are idempotent
    on identity, not value — same callable is what dedupes)."""
    # Use an isolated bus instance so we don't pollute the singleton.
    isolated = EventBus()
    isolated.subscribe("meeting.scheduled", notif_bridge._on_meeting_scheduled)
    isolated.subscribe("meeting.scheduled", notif_bridge._on_meeting_scheduled)
    # Two registrations → two entries (subscribe is append-only); the
    # production register_* function is called once from mount_cloud, so
    # this matters only if someone mounts twice. Documenting the behaviour
    # rather than enforcing dedup.
    assert len(isolated._handlers["meeting.scheduled"]) == 2


# ---------------------------------------------------------------------------
# Calendar bridge — auto-create Meeting from a calendar event with a URL
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_event_doc_class(monkeypatch):
    """Patch _EventDoc.find_one to return a SimpleNamespace stand-in.

    Avoids initialising the calendar package's Beanie docs in the cloud
    test fixture (they live in a sibling enterprise package). The fields
    the bridge reads — description, location, title, starts_at, ends_at,
    created_by_user_id — are all simple attributes, so a namespace works.
    """
    from types import SimpleNamespace

    store: dict[str, SimpleNamespace] = {}

    async def fake_find_one(query):
        # Bridge calls _EventDoc.find_one({"workspace": ws, "_id": eid}).
        return store.get(query.get("_id"))

    class _Stub:
        find_one = staticmethod(fake_find_one)

    monkeypatch.setattr("pocketpaw_ee.calendar.models._EventDoc", _Stub)
    return store


async def test_calendar_event_with_zoom_url_creates_meeting(
    mongo_db, fake_event_doc_class, patched_notifications
):
    """A calendar.event.created with a Zoom URL should insert a Meeting
    with source='recall', provider='zoom', and the detected join URL —
    plus emit meeting.scheduled for the notification bridge to pick up."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    # Register notification listeners so the meeting.scheduled emit reaches
    # notifications_service.create — proves the calendar→notification chain.
    notif_bridge.register_meeting_notification_listeners()

    starts_at = datetime.now(UTC) + timedelta(hours=1)
    fake_event_doc_class["evt-zoom"] = SimpleNamespace(
        id="evt-zoom",
        title="Sprint demo",
        description="Join at https://us02web.zoom.us/j/12345 — please be on time",
        location=None,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=45),
        created_by_user_id="user-organizer",
    )

    await calendar_bridge._on_calendar_event_created(
        {"event_id": "evt-zoom", "workspace_id": "ws-1"}
    )

    docs = await _MeetingDoc.find({"workspace": "ws-1"}).to_list()
    assert len(docs) == 1
    meeting = docs[0]
    assert meeting.source == "recall"
    assert meeting.provider == "zoom"
    assert meeting.join_url == "https://us02web.zoom.us/j/12345"
    assert meeting.title == "Sprint demo"
    assert meeting.raw_provider_payload["calendar_event_id"] == "evt-zoom"
    assert meeting.raw_provider_payload["auto_created_by"] == "calendar_bridge"
    assert meeting.created_by_user_id == "user-organizer"
    # meeting.scheduled fan-out fires for the notification bridge:
    assert patched_notifications.call_count == 1


async def test_calendar_event_without_meeting_url_does_nothing(mongo_db, fake_event_doc_class):
    """Calendar events with no Zoom/Meet URL in any field should NOT
    create a Meeting — most calendar events are just appointments."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    fake_event_doc_class["evt-lunch"] = SimpleNamespace(
        id="evt-lunch",
        title="Lunch",
        description="Sushi place on 5th",
        location=None,
        starts_at=datetime.now(UTC),
        ends_at=datetime.now(UTC) + timedelta(hours=1),
        created_by_user_id="user-1",
    )

    await calendar_bridge._on_calendar_event_created(
        {"event_id": "evt-lunch", "workspace_id": "ws-1"}
    )

    assert await _MeetingDoc.find({"workspace": "ws-1"}).count() == 0


async def test_calendar_auto_create_is_idempotent(mongo_db, fake_event_doc_class):
    """Firing calendar.event.created twice for the same event id should
    create exactly one Meeting — Google's sync can resend events on retry."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    starts_at = datetime.now(UTC) + timedelta(hours=1)
    fake_event_doc_class["evt-dup"] = SimpleNamespace(
        id="evt-dup",
        title="Standup",
        description="https://meet.google.com/abc-defg-hij",
        location=None,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=30),
        created_by_user_id="user-1",
    )

    data = {"event_id": "evt-dup", "workspace_id": "ws-1"}
    await calendar_bridge._on_calendar_event_created(data)
    await calendar_bridge._on_calendar_event_created(data)

    assert await _MeetingDoc.find({"workspace": "ws-1"}).count() == 1


async def test_calendar_delete_cancels_auto_created_meeting(mongo_db):
    """Deleting the calendar event should cancel the linked meeting
    (status='cancelled') so it stops showing up as upcoming."""
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    meeting = _MeetingDoc(
        workspace="ws-1",
        source="recall",
        provider="zoom",
        provider_meeting_id="",
        title="Auto",
        join_url="https://zoom.us/j/1",
        status="scheduled",
        raw_provider_payload={
            "calendar_event_id": "evt-1",
            "auto_created_by": "calendar_bridge",
        },
        created_by_user_id="user-1",
    )
    await meeting.insert()

    await calendar_bridge._on_calendar_event_deleted({"event_id": "evt-1", "workspace_id": "ws-1"})

    refreshed = await _MeetingDoc.get(meeting.id)
    assert refreshed.status == "cancelled"
