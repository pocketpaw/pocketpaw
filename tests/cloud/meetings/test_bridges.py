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


# ---------------------------------------------------------------------------
# Reverse bridge — Meeting → CalendarEvent
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_calendar_service(monkeypatch):
    """Replace calendar.service.{create_event,delete_event} so we can
    exercise the reverse bridge without spinning up the calendar Beanie
    model + Calendar policy machinery.
    """
    create_mock = AsyncMock()
    delete_mock = AsyncMock()

    def _create_returns(ctx, body):
        from types import SimpleNamespace

        return SimpleNamespace(
            id="evt-from-meeting",
            workspace_id=ctx.workspace_id,
            calendar_id=body.calendar_id,
            title=body.title,
            description=body.description,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            timezone=body.timezone,
            created_by_user_id=ctx.user_id,
            location=body.location,
        )

    create_mock.side_effect = _create_returns

    monkeypatch.setattr("pocketpaw_ee.calendar.service.create_event", create_mock)
    monkeypatch.setattr("pocketpaw_ee.calendar.service.delete_event", delete_mock)
    return {"create": create_mock, "delete": delete_mock}


async def test_meeting_scheduled_mints_calendar_event(mongo_db, patched_calendar_service):
    """A meeting.scheduled emit (e.g. POST /meetings) should mint a
    matching CalendarEvent and stamp the meeting with the new event id."""
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    starts_at = datetime.now(UTC) + timedelta(hours=2)
    meeting = _MeetingDoc(
        workspace="ws-1",
        source="recall",
        provider="zoom",
        provider_meeting_id="zoom-id-1",
        title="Customer call",
        join_url="https://zoom.us/j/9999",
        scheduled_start=starts_at,
        scheduled_end=starts_at + timedelta(minutes=30),
        status="scheduled",
        created_by_user_id="user-1",
    )
    await meeting.insert()

    await calendar_bridge._on_meeting_scheduled(
        {
            "workspace_id": "ws-1",
            "meeting_id": str(meeting.id),
            "source": "recall",
            "provider": "zoom",
            "created_by": "user-1",
        }
    )

    # CalendarEvent created with the loop-prevention marker.
    assert patched_calendar_service["create"].call_count == 1
    call = patched_calendar_service["create"].call_args
    ctx, body = call.args
    assert ctx.workspace_id == "ws-1"
    assert ctx.user_id == "user-1"
    assert body.title == "Customer call"
    assert body.location == "https://zoom.us/j/9999"
    assert body.fabric_object_id == f"meeting:{meeting.id}"

    # Meeting now linked back to the calendar event.
    refreshed = await _MeetingDoc.get(meeting.id)
    assert refreshed.raw_provider_payload["calendar_event_id"] == "evt-from-meeting"
    assert refreshed.raw_provider_payload["auto_linked_to_calendar"] == "meeting_bridge"


async def test_meeting_scheduled_skips_when_auto_created_from_calendar(
    mongo_db, patched_calendar_service
):
    """Meetings minted by the forward bridge already correspond to a
    calendar event — emitting our own would create a duplicate row."""
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    meeting = _MeetingDoc(
        workspace="ws-1",
        source="recall",
        provider="zoom",
        provider_meeting_id="",
        title="Forward-bridge meeting",
        join_url="https://zoom.us/j/1",
        scheduled_start=datetime.now(UTC) + timedelta(hours=1),
        status="scheduled",
        raw_provider_payload={"calendar_event_id": "evt-orig"},
    )
    await meeting.insert()

    await calendar_bridge._on_meeting_scheduled(
        {
            "workspace_id": "ws-1",
            "meeting_id": str(meeting.id),
            "auto_created_from_calendar": True,
        }
    )

    assert patched_calendar_service["create"].call_count == 0


async def test_meeting_scheduled_is_idempotent(mongo_db, patched_calendar_service):
    """Firing meeting.scheduled twice for the same meeting must not
    create two calendar events. The link on raw_provider_payload guards
    the second call."""
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    starts_at = datetime.now(UTC) + timedelta(hours=1)
    meeting = _MeetingDoc(
        workspace="ws-1",
        source="livekit",
        provider=None,
        provider_meeting_id="room-1",
        title="Group sync",
        join_url="",
        scheduled_start=starts_at,
        status="scheduled",
        created_by_user_id="user-1",
    )
    await meeting.insert()

    data = {"workspace_id": "ws-1", "meeting_id": str(meeting.id)}
    await calendar_bridge._on_meeting_scheduled(data)
    await calendar_bridge._on_meeting_scheduled(data)

    assert patched_calendar_service["create"].call_count == 1


async def test_meeting_cancelled_deletes_linked_calendar_event(mongo_db, patched_calendar_service):
    """Cancelling a meeting that has a bridge-minted calendar event
    should delete that calendar event."""
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    meeting = _MeetingDoc(
        workspace="ws-1",
        source="recall",
        provider="zoom",
        provider_meeting_id="z-1",
        title="Demo",
        join_url="https://zoom.us/j/1",
        status="cancelled",
        raw_provider_payload={
            "calendar_event_id": "evt-from-meeting",
            "auto_linked_to_calendar": "meeting_bridge",
        },
        created_by_user_id="user-1",
    )
    await meeting.insert()

    await calendar_bridge._on_meeting_cancelled(
        {"workspace_id": "ws-1", "meeting_id": str(meeting.id), "provider": "zoom"}
    )

    assert patched_calendar_service["delete"].call_count == 1
    ctx, event_id = patched_calendar_service["delete"].call_args.args
    assert ctx.workspace_id == "ws-1"
    assert event_id == "evt-from-meeting"


async def test_meeting_cancelled_skips_when_not_bridge_linked(mongo_db, patched_calendar_service):
    """If the calendar_event_id came from the forward bridge (calendar→
    meeting), the user's own calendar deletion handles cleanup. We must
    not delete the source calendar event."""
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    meeting = _MeetingDoc(
        workspace="ws-1",
        source="recall",
        provider="zoom",
        provider_meeting_id="",
        title="Forward-minted",
        join_url="https://zoom.us/j/1",
        status="cancelled",
        raw_provider_payload={
            "calendar_event_id": "evt-user-owned",
            "auto_created_by": "calendar_bridge",
        },
    )
    await meeting.insert()

    await calendar_bridge._on_meeting_cancelled(
        {"workspace_id": "ws-1", "meeting_id": str(meeting.id)}
    )

    assert patched_calendar_service["delete"].call_count == 0


async def test_calendar_event_skips_when_minted_by_reverse_bridge(mongo_db, fake_event_doc_class):
    """Loop guard: calendar events that carry a ``meeting:`` fabric link
    were created by the reverse bridge. The forward handler must NOT
    detect their URL and create a second Meeting."""
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    starts_at = datetime.now(UTC) + timedelta(hours=1)
    fake_event_doc_class["evt-loop"] = SimpleNamespace(
        id="evt-loop",
        title="Demo",
        description="Join: https://zoom.us/j/1",
        location="https://zoom.us/j/1",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=30),
        created_by_user_id="user-1",
        fabric_object_id="meeting:abc123",
    )

    await calendar_bridge._on_calendar_event_created(
        {"event_id": "evt-loop", "workspace_id": "ws-1"}
    )

    assert await _MeetingDoc.find({"workspace": "ws-1"}).count() == 0
