# tests/ee/calendar/test_policy.py — calendar access-policy tests.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H1 + H-NEW-1).
#
# Changes:
# - Added unit tests for policy.check_event_modify (creator allowed,
#   non-creator denied, cross-workspace denied, admin path TODO'd).
# - _PolicyFakeEventDoc now defaults created_by_user_id so existing
#   tests keep working; H-NEW-1 tests pass explicit ids.
#
# Exercises the policy.check_calendar_read / check_calendar_write /
# can_read_calendar / check_event_modify helpers across the
# cross-workspace + within-workspace matrix. Also runs an end-to-end
# check that service.create_event, update_event, delete_event, get_event,
# list_events, detect_conflicts, and get_freebusy all surface Forbidden
# when a non-owner / non-shared / non-creator user touches a private or
# shared calendar.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from bson import ObjectId
from pocketpaw_ee.calendar import policy
from pocketpaw_ee.calendar import service as service_module
from pocketpaw_ee.calendar._context import RequestContext
from pocketpaw_ee.calendar.domain import Calendar, CalendarVisibility
from pocketpaw_ee.calendar.dto import (
    CreateEventRequest,
    FreeBusyRequest,
    ListEventsRequest,
    UpdateEventRequest,
)
from pocketpaw_ee.cloud.shared.errors import Forbidden

# ---------------------------------------------------------------------------
# Helpers — minimal Calendar + RequestContext factories.
# ---------------------------------------------------------------------------


def _make_calendar(
    *,
    owner: str = "alice",
    workspace_id: str = "ws-1",
    visibility: CalendarVisibility = CalendarVisibility.PUBLIC_TO_WORKSPACE,
    shared_with: list[str] | None = None,
) -> Calendar:
    return Calendar(
        id="cal-1",
        workspace_id=workspace_id,
        name="Team",
        owner_user_id=owner,
        timezone="UTC",
        visibility=visibility,
        shared_with_user_ids=shared_with or [],
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def _ctx(user_id: str = "alice", workspace_id: str = "ws-1") -> RequestContext:
    return RequestContext(workspace_id=workspace_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Unit tests — policy functions in isolation.
# ---------------------------------------------------------------------------


class TestCheckCalendarRead:
    def test_owner_can_read(self):
        cal = _make_calendar(owner="alice")
        # No exception means access granted.
        policy.check_calendar_read(_ctx("alice"), cal)

    def test_workspace_public_grants_read_to_non_owner(self):
        cal = _make_calendar(
            owner="alice",
            visibility=CalendarVisibility.PUBLIC_TO_WORKSPACE,
        )
        policy.check_calendar_read(_ctx("bob"), cal)

    def test_private_denies_non_owner(self):
        cal = _make_calendar(owner="alice", visibility=CalendarVisibility.PRIVATE)
        with pytest.raises(Forbidden):
            policy.check_calendar_read(_ctx("bob"), cal)

    def test_shared_grants_to_listed_user(self):
        cal = _make_calendar(
            owner="alice",
            visibility=CalendarVisibility.SHARED_WITH_USERS,
            shared_with=["bob"],
        )
        policy.check_calendar_read(_ctx("bob"), cal)

    def test_shared_denies_unlisted_user(self):
        cal = _make_calendar(
            owner="alice",
            visibility=CalendarVisibility.SHARED_WITH_USERS,
            shared_with=["charlie"],
        )
        with pytest.raises(Forbidden):
            policy.check_calendar_read(_ctx("bob"), cal)

    def test_cross_workspace_always_denied(self):
        """Hard tenant boundary — even the owner can't read across workspaces."""
        cal = _make_calendar(owner="alice", workspace_id="ws-other")
        with pytest.raises(Forbidden):
            policy.check_calendar_read(_ctx("alice", workspace_id="ws-1"), cal)


class TestCheckCalendarWrite:
    def test_owner_can_write(self):
        cal = _make_calendar(owner="alice")
        policy.check_calendar_write(_ctx("alice"), cal)

    def test_public_does_not_grant_write(self):
        """Workspace-public is read-only by default — promote to shared
        for write."""
        cal = _make_calendar(
            owner="alice",
            visibility=CalendarVisibility.PUBLIC_TO_WORKSPACE,
        )
        with pytest.raises(Forbidden):
            policy.check_calendar_write(_ctx("bob"), cal)

    def test_shared_grants_write(self):
        cal = _make_calendar(
            owner="alice",
            visibility=CalendarVisibility.SHARED_WITH_USERS,
            shared_with=["bob"],
        )
        policy.check_calendar_write(_ctx("bob"), cal)

    def test_private_denies_non_owner_write(self):
        cal = _make_calendar(owner="alice", visibility=CalendarVisibility.PRIVATE)
        with pytest.raises(Forbidden):
            policy.check_calendar_write(_ctx("bob"), cal)

    def test_cross_workspace_write_denied(self):
        cal = _make_calendar(owner="alice", workspace_id="ws-other")
        with pytest.raises(Forbidden):
            policy.check_calendar_write(_ctx("alice", workspace_id="ws-1"), cal)


class TestCanReadCalendar:
    """can_read_calendar is the predicate form — returns bool, no raise."""

    def test_returns_true_for_owner(self):
        cal = _make_calendar(owner="alice")
        assert policy.can_read_calendar(_ctx("alice"), cal) is True

    def test_returns_false_for_private_non_owner(self):
        cal = _make_calendar(owner="alice", visibility=CalendarVisibility.PRIVATE)
        assert policy.can_read_calendar(_ctx("bob"), cal) is False

    def test_returns_false_cross_workspace(self):
        cal = _make_calendar(owner="alice", workspace_id="ws-other")
        assert policy.can_read_calendar(_ctx("alice", workspace_id="ws-1"), cal) is False


class TestCheckEventModify:
    """check_event_modify is the H-NEW-1 gate — creator-or-admin only.

    Synthetic-default Calendar makes check_calendar_write trivially pass
    for every workspace member, so update_event / delete_event must call
    this on the event itself to keep cross-user modify access closed.
    """

    def test_event_modify_creator_allowed(self, event_factory):
        """Alice created the event → alice can modify."""
        event = event_factory(
            workspace_id="ws-1",
            created_by_user_id="alice",
        )
        # No exception means access granted.
        policy.check_event_modify(_ctx("alice", workspace_id="ws-1"), event)

    def test_event_modify_non_creator_denied(self, event_factory):
        """Bob, same workspace, is NOT the creator → Forbidden."""
        event = event_factory(
            workspace_id="ws-1",
            created_by_user_id="alice",
        )
        with pytest.raises(Forbidden) as exc_info:
            policy.check_event_modify(_ctx("bob", workspace_id="ws-1"), event)
        assert "event.modify_denied" in str(exc_info.value)

    def test_event_modify_cross_workspace_denied(self, event_factory):
        """Defensive guard: even if the caller IS the creator, a workspace
        mismatch fails closed. The tenant filter upstream usually catches
        this first via 404, but this assertion documents the policy itself."""
        event = event_factory(
            workspace_id="ws-other",
            created_by_user_id="alice",
        )
        with pytest.raises(Forbidden) as exc_info:
            policy.check_event_modify(_ctx("alice", workspace_id="ws-1"), event)
        assert "event.modify_denied" in str(exc_info.value)

    @pytest.mark.skip(
        reason=(
            "TODO(h-new-1-admin): workspace-admin override not yet plumbed "
            "through RequestContext. Once the calendar context carries a "
            "role/permissions field, this test asserts an admin (non-creator) "
            "can still modify. Tracked alongside the policy.check_event_modify "
            "TODO."
        )
    )
    def test_event_modify_admin_allowed(self, event_factory):  # pragma: no cover
        """Workspace admins should bypass the creator check. Skipped until
        the role plumbing lands."""
        event = event_factory(
            workspace_id="ws-1",
            created_by_user_id="alice",
        )
        # Future shape: _ctx("bob-admin") would resolve a role of "admin".
        policy.check_event_modify(_ctx("bob-admin", workspace_id="ws-1"), event)


# ---------------------------------------------------------------------------
# Integration tests — policy wired into service.* operations.
# ---------------------------------------------------------------------------
#
# We patch _CalendarDoc + _EventDoc on the service module so we don't need
# Beanie at all. The seed: a "private" calendar owned by alice, plus a few
# events. Non-owner bob should not be able to read or write those events.


class _PolicyFakeCalDoc:
    def __init__(self, **fields: Any) -> None:
        self.id = fields.get("_id", ObjectId())
        for k, v in fields.items():
            if k == "_id":
                continue
            setattr(self, k, v)
        # Domain defaults.
        self.color = getattr(self, "color", "#0A84FF")
        self.shared_with_user_ids = getattr(self, "shared_with_user_ids", [])
        self.created_at = getattr(self, "created_at", datetime.now(UTC))
        self.updated_at = getattr(self, "updated_at", datetime.now(UTC))


class _PolicyFakeCalStore:
    """Calendar store that returns a fixed _CalendarDoc per (id, workspace)
    lookup, or None to force the synthetic-default fallback."""

    def __init__(self) -> None:
        self.rows: list[_PolicyFakeCalDoc] = []

    async def find_one(self, query: dict[str, Any]) -> _PolicyFakeCalDoc | None:
        cal_id = query.get("_id")
        workspace = query.get("workspace")
        for r in self.rows:
            if (
                # service.py passes either a PydanticObjectId or the raw str
                (str(r.id) == str(cal_id) or r.id == cal_id) and r.workspace == workspace
            ):
                return r
        return None


class _PolicyFakeEventDoc:
    def __init__(self, **fields: Any) -> None:
        self.id = fields.pop("_id", ObjectId())
        for k, v in fields.items():
            setattr(self, k, v)
        self.description = getattr(self, "description", "")
        self.attendees = getattr(self, "attendees", [])
        self.recurrence = getattr(self, "recurrence", None)
        self.location = getattr(self, "location", None)
        self.fabric_object_id = getattr(self, "fabric_object_id", None)
        self.source_connector = getattr(self, "source_connector", None)
        self.source_external_id = getattr(self, "source_external_id", None)
        self.created_at = getattr(self, "created_at", datetime.now(UTC))
        self.updated_at = getattr(self, "updated_at", datetime.now(UTC))
        # H-NEW-1: real _EventDoc requires it. Default to the test's owner
        # ("alice") so existing tests where alice is both calendar owner
        # and event creator stay green.
        self.created_by_user_id = getattr(self, "created_by_user_id", "alice")

    async def insert(self) -> None:
        pass

    async def save(self) -> None:
        pass

    async def delete(self) -> None:
        pass


class _PolicyFakeEventQuery:
    def __init__(self, docs: list[_PolicyFakeEventDoc]) -> None:
        self._docs = docs
        self._limit: int | None = None

    def limit(self, n: int) -> _PolicyFakeEventQuery:
        self._limit = n
        return self

    async def to_list(self) -> list[_PolicyFakeEventDoc]:
        if self._limit is not None:
            return self._docs[: self._limit]
        return self._docs


class _PolicyFakeEventStore:
    def __init__(self) -> None:
        self.rows: list[_PolicyFakeEventDoc] = []

    def find(self, *args: Any) -> _PolicyFakeEventQuery:
        # Return everything — the per-event policy check filters.
        return _PolicyFakeEventQuery(list(self.rows))

    async def find_one(self, *args: Any) -> _PolicyFakeEventDoc | None:
        # The service compares by id; pull the requested id off the args.
        for arg in args:
            if hasattr(arg, "field") and arg.field == "id_str":
                target_id = arg.value
                for r in self.rows:
                    if str(r.id) == str(target_id):
                        return r
        # First-match fallback (used by single-doc tests).
        return self.rows[0] if self.rows else None


class _PolicyFieldEq:
    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value


class _PolicyFieldProxy:
    def __init__(self, field: str) -> None:
        self._field = field

    def __eq__(self, other: Any) -> _PolicyFieldEq:  # type: ignore[override]
        return _PolicyFieldEq(self._field, other)


@pytest.fixture
def patch_stores(monkeypatch) -> tuple[_PolicyFakeCalStore, _PolicyFakeEventStore]:
    """Install fake stores for both _CalendarDoc and _EventDoc."""
    cal_store = _PolicyFakeCalStore()
    event_store = _PolicyFakeEventStore()
    monkeypatch.setattr(service_module, "_CalendarDoc", cal_store)
    monkeypatch.setattr(service_module, "_EventDoc", event_store)
    # service uses ``_EventDoc.id == oid`` and ``.workspace == ...`` via the
    # _get_event_doc_or_404 helper. Mirror what test_service.py does.
    event_store.id = _PolicyFieldProxy("id_str")  # type: ignore[attr-defined]
    event_store.workspace = _PolicyFieldProxy("workspace")  # type: ignore[attr-defined]
    return cal_store, event_store


@pytest.fixture
def bus_spy(monkeypatch):
    """No-op bus so emits don't blow up."""
    from pocketpaw_ee.cloud.shared.events import event_bus

    async def _spy(topic: str, data: dict) -> None:
        pass

    monkeypatch.setattr(event_bus, "emit", _spy)


def _seed_calendar(
    store: _PolicyFakeCalStore,
    *,
    cal_id: str = "cal-1",
    workspace: str = "ws-1",
    owner: str = "alice",
    visibility: str = "private",
    shared_with: list[str] | None = None,
) -> _PolicyFakeCalDoc:
    doc = _PolicyFakeCalDoc(
        _id=cal_id,
        workspace=workspace,
        name="Team",
        owner_user_id=owner,
        timezone="UTC",
        visibility=visibility,
        shared_with_user_ids=shared_with or [],
    )
    store.rows.append(doc)
    return doc


def _seed_event(
    store: _PolicyFakeEventStore,
    *,
    workspace: str = "ws-1",
    calendar_id: str = "cal-1",
    starts: datetime | None = None,
    ends: datetime | None = None,
    attendees: list[dict] | None = None,
    created_by_user_id: str = "alice",
) -> _PolicyFakeEventDoc:
    doc = _PolicyFakeEventDoc(
        workspace=workspace,
        calendar_id=calendar_id,
        title="Standup",
        starts_at=starts or datetime(2026, 5, 19, 9, 0),
        ends_at=ends or datetime(2026, 5, 19, 9, 30),
        timezone="UTC",
        attendees=attendees or [],
        # H-NEW-1: default matches the alice-owned calendar fixture so
        # existing tests stay green. Pass explicitly for non-creator
        # scenarios.
        created_by_user_id=created_by_user_id,
    )
    # Mirror id as a separate field so _PolicyFakeEventStore.find_one
    # can match by `_FieldEq("id_str", ...)`.
    doc.id_str = doc.id
    store.rows.append(doc)
    return doc


# ---------------------------------------------------------------------------


async def test_non_owner_cannot_get_event_on_private_calendar(patch_stores, bus_spy):
    cal_store, event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")
    event = _seed_event(event_store)

    with pytest.raises(Forbidden):
        await service_module.get_event(_ctx("bob"), str(event.id))


async def test_non_owner_cannot_update_event_on_private_calendar(patch_stores, bus_spy):
    cal_store, event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")
    event = _seed_event(event_store)

    with pytest.raises(Forbidden):
        await service_module.update_event(
            _ctx("bob"),
            str(event.id),
            UpdateEventRequest(title="Hacked"),
        )


async def test_non_owner_cannot_delete_event_on_private_calendar(patch_stores, bus_spy):
    cal_store, event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")
    event = _seed_event(event_store)

    with pytest.raises(Forbidden):
        await service_module.delete_event(_ctx("bob"), str(event.id))


async def test_non_owner_cannot_create_event_on_private_calendar(patch_stores, bus_spy):
    cal_store, _event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")

    with pytest.raises(Forbidden):
        await service_module.create_event(
            _ctx("bob"),
            CreateEventRequest(
                calendar_id="cal-1",
                title="Sneaky",
                starts_at=datetime(2026, 5, 19, 9, 0),
                ends_at=datetime(2026, 5, 19, 10, 0),
                timezone="UTC",
            ),
        )


async def test_non_owner_cannot_create_event_on_public_calendar(patch_stores, bus_spy):
    """Public calendars are READ-only by default — write requires explicit share."""
    cal_store, _event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="public_to_workspace")

    with pytest.raises(Forbidden):
        await service_module.create_event(
            _ctx("bob"),
            CreateEventRequest(
                calendar_id="cal-1",
                title="Should be denied",
                starts_at=datetime(2026, 5, 19, 9, 0),
                ends_at=datetime(2026, 5, 19, 10, 0),
                timezone="UTC",
            ),
        )


async def test_owner_can_update_event_on_private_calendar(patch_stores, bus_spy):
    cal_store, event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")
    event = _seed_event(event_store)

    result = await service_module.update_event(
        _ctx("alice"),
        str(event.id),
        UpdateEventRequest(title="Owner-driven update"),
    )
    assert result.title == "Owner-driven update"


async def test_shared_user_can_update_their_own_event(patch_stores, bus_spy):
    """H-NEW-1 tightening: a shared user can WRITE to the calendar (e.g.,
    create new events), but on UPDATE they must also be the event creator.
    Here bob is shared on alice's calendar AND created the event himself,
    so the update succeeds."""
    cal_store, event_store = patch_stores
    _seed_calendar(
        cal_store,
        owner="alice",
        visibility="shared_with_users",
        shared_with=["bob"],
    )
    # Bob created this event (not alice). check_calendar_write passes
    # because bob is in shared_with; check_event_modify passes because
    # bob == created_by_user_id.
    event = _seed_event(event_store, created_by_user_id="bob")

    result = await service_module.update_event(
        _ctx("bob"),
        str(event.id),
        UpdateEventRequest(title="Shared user update"),
    )
    assert result.title == "Shared user update"


async def test_shared_user_cannot_update_someone_elses_event(patch_stores, bus_spy):
    """H-NEW-1: even with calendar-write access, a shared user cannot edit
    events they didn't create. The event-level gate fires."""
    cal_store, event_store = patch_stores
    _seed_calendar(
        cal_store,
        owner="alice",
        visibility="shared_with_users",
        shared_with=["bob"],
    )
    # Event was created by alice. Bob has calendar-write but no
    # event-modify access.
    event = _seed_event(event_store, created_by_user_id="alice")

    with pytest.raises(Forbidden) as exc_info:
        await service_module.update_event(
            _ctx("bob"),
            str(event.id),
            UpdateEventRequest(title="Bob tries to overwrite alice's event"),
        )
    assert "event.modify_denied" in str(exc_info.value)


async def test_list_events_filters_inaccessible_calendars(patch_stores):
    """Events on a private calendar I don't own are silently dropped from list."""
    cal_store, event_store = patch_stores
    # alice's private calendar
    _seed_calendar(
        cal_store,
        cal_id="cal-private",
        owner="alice",
        visibility="private",
    )
    # bob's public calendar
    _seed_calendar(
        cal_store,
        cal_id="cal-public",
        owner="bob",
        visibility="public_to_workspace",
    )

    _seed_event(event_store, calendar_id="cal-private")
    _seed_event(event_store, calendar_id="cal-public")

    body = ListEventsRequest(
        starts_after=datetime(2026, 5, 1),
        starts_before=datetime(2026, 6, 1),
    )
    # Bob lists — only sees the public calendar event.
    result = await service_module.list_events(_ctx("bob"), body)
    assert result.total == 1
    assert result.events[0].calendar_id == "cal-public"


async def test_list_events_hard_403_when_calendar_id_inaccessible(patch_stores):
    """When the caller filters by a specific calendar they can't read,
    we return a 403 (clearer UX than an empty list)."""
    cal_store, _event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")

    body = ListEventsRequest(
        calendar_id="cal-1",
        starts_after=datetime(2026, 5, 1),
        starts_before=datetime(2026, 6, 1),
    )
    with pytest.raises(Forbidden):
        await service_module.list_events(_ctx("bob"), body)


async def test_freebusy_denied_for_emails_on_inaccessible_calendar(patch_stores):
    """H2 — alice's private calendar shouldn't leak as an availability oracle
    to bob, even if bob asks for alice's email directly."""
    cal_store, event_store = patch_stores
    _seed_calendar(
        cal_store,
        cal_id="cal-private",
        owner="alice",
        visibility="private",
    )
    _seed_event(
        event_store,
        calendar_id="cal-private",
        attendees=[{"email": "alice@example.com"}],
    )

    # Bob requests freebusy for alice. The only event matching alice is on
    # a calendar bob can't read — so the unknown-attendee gate fires.
    with pytest.raises(Exception) as exc_info:  # ValidationError
        await service_module.get_freebusy(
            _ctx("bob"),
            FreeBusyRequest(
                attendee_emails=["alice@example.com"],
                starts_at=datetime(2026, 5, 19, 0, 0),
                ends_at=datetime(2026, 5, 20, 0, 0),
            ),
        )
    # Confirm it surfaces as our cloud ValidationError, not a 500.
    assert "calendar.unknown_attendee" in str(exc_info.value)


async def test_freebusy_succeeds_for_emails_on_accessible_calendar(patch_stores, monkeypatch):
    """Sanity inverse: bob CAN query freebusy for alice if the event lives
    on a workspace-public calendar."""
    cal_store, event_store = patch_stores
    _seed_calendar(
        cal_store,
        cal_id="cal-public",
        owner="alice",
        visibility="public_to_workspace",
    )
    _seed_event(
        event_store,
        calendar_id="cal-public",
        attendees=[{"email": "alice@example.com"}],
    )

    # compute_freebusy issues its own _EventDoc.find call; patch the
    # symbol the freebusy module sees so we don't need Beanie.
    from pocketpaw_ee.calendar import freebusy as fb_module

    monkeypatch.setattr(fb_module, "_EventDoc", event_store)

    resp = await service_module.get_freebusy(
        _ctx("bob"),
        FreeBusyRequest(
            attendee_emails=["alice@example.com"],
            starts_at=datetime(2026, 5, 19, 0, 0),
            ends_at=datetime(2026, 5, 20, 0, 0),
        ),
    )
    assert len(resp.freebusy) == 1
    assert resp.freebusy[0].attendee_email == "alice@example.com"


async def test_detect_conflicts_blocked_for_non_owner_on_private_calendar(
    patch_stores, bus_spy, monkeypatch
):
    """Conflict detection scans the workspace; non-owner with no read
    access to the event's calendar must not be allowed to invoke it."""
    cal_store, event_store = patch_stores
    _seed_calendar(cal_store, owner="alice", visibility="private")
    event = _seed_event(event_store)

    # find_conflicts is real but we don't care — should fail before that.
    async def _no_conflicts(workspace_id, target):
        return []

    monkeypatch.setattr(service_module, "find_conflicts", _no_conflicts)

    with pytest.raises(Forbidden):
        await service_module.detect_conflicts(_ctx("bob"), str(event.id))


# ---------------------------------------------------------------------------
# H-NEW-1: synthetic-default Calendar must not bypass event-level authz.
# ---------------------------------------------------------------------------
#
# When _CalendarDoc has no row for a calendar_id, service._load_calendar
# returns a synthetic Calendar with owner_user_id = ctx.user_id +
# visibility = public-to-workspace. That makes check_calendar_write a
# no-op for every caller. These tests assert that policy.check_event_modify
# still blocks non-creators from mutating other users' events on that path.


async def test_h_new_1_update_blocked_on_synthetic_calendar(patch_stores, bus_spy):
    """Alice creates an event on a calendar with no backing _CalendarDoc
    row (synthetic-default path). Bob (same workspace) cannot update it
    even though check_calendar_write trivially passes."""
    _cal_store, event_store = patch_stores
    # IMPORTANT: NO _seed_calendar — leaving the store empty forces the
    # service to fall through to the synthetic default.
    event = _seed_event(event_store, created_by_user_id="alice")

    with pytest.raises(Forbidden) as exc_info:
        await service_module.update_event(
            _ctx("bob"),
            str(event.id),
            UpdateEventRequest(title="Should be denied"),
        )
    assert "event.modify_denied" in str(exc_info.value)


async def test_h_new_1_delete_blocked_on_synthetic_calendar(patch_stores, bus_spy):
    """Same shape — delete must also fail when bob tries to drop alice's
    event on a synthetic-default Calendar."""
    _cal_store, event_store = patch_stores
    event = _seed_event(event_store, created_by_user_id="alice")

    with pytest.raises(Forbidden) as exc_info:
        await service_module.delete_event(_ctx("bob"), str(event.id))
    assert "event.modify_denied" in str(exc_info.value)


async def test_h_new_1_creator_can_still_update_on_synthetic_calendar(patch_stores, bus_spy):
    """The fix is creator-equality, not "synthetic = read-only". Alice (the
    creator) still has the happy path."""
    _cal_store, event_store = patch_stores
    event = _seed_event(event_store, created_by_user_id="alice")

    resp = await service_module.update_event(
        _ctx("alice"),
        str(event.id),
        UpdateEventRequest(title="Owner-edit ok"),
    )
    assert resp.title == "Owner-edit ok"
