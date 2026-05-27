# tests/ee/calendar/test_service.py — service-layer tests.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 + H-NEW-1).
#
# Changes:
# - Added ``fake_calendar_store`` (auto-applied) that patches
#   ``_CalendarDoc`` to a stub returning ``None`` on every ``find_one``.
#   The service then falls back to its synthetic default calendar so
#   existing tests keep their pre-#1142 behaviour (within-workspace
#   reads/writes by the caller still pass).
# - update_event payload assertion lives in test_policy.py /
#   test_update_event_changed_fields below — the bus event now carries
#   ``changed_fields`` (names only), not raw content.
# - H-NEW-1: ``_FakeDoc`` now defaults ``created_by_user_id`` to
#   "user-test" (matching ctx()), and ``_new_doc`` accepts an explicit
#   override so non-creator scenarios can be set up. Four new tests cover
#   the synthetic-calendar modify-authz behaviour: creator can update +
#   delete, non-creator can't, and create still works for any workspace
#   member.
#
# Approach: we don't spin up real Mongo. Instead we replace _EventDoc at
# the class level with a fake that mimics the small slice of Beanie we
# actually use (find, find_one, insert, save, delete). That gives us
# fast, deterministic tests and forces us to keep the surface narrow —
# if the service ever reaches into Beanie API we don't fake, the test
# fails loudly and we either fake more or simplify the service.

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from bson import ObjectId
from pocketpaw_ee.calendar import service as service_module
from pocketpaw_ee.calendar.dto import (
    CreateEventRequest,
    FreeBusyRequest,
    ListEventsRequest,
    UpdateEventRequest,
)
from pocketpaw_ee.calendar.events import (
    TOPIC_CONFLICT_DETECTED,
    TOPIC_EVENT_CREATED,
    TOPIC_EVENT_DELETED,
    TOPIC_EVENT_UPDATED,
)
from pocketpaw_ee.cloud.shared.errors import NotFound, ValidationError

# ---------------------------------------------------------------------------
# Fake Beanie doc — minimum viable replacement.
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory replacement for the _EventDoc class. Carries the same name
    as a Document type, so service code that calls _EventDoc.find_one works
    by going through this stand-in."""

    def __init__(self) -> None:
        self.rows: dict[str, _FakeDoc] = {}

    def __call__(self, **fields: Any) -> _FakeDoc:
        """Constructor — service does ``_EventDoc(workspace=...)``."""
        doc = _FakeDoc(store=self, **fields)
        return doc

    def reset(self) -> None:
        self.rows.clear()

    # Beanie-style query API ---------------------------------------------------

    def find(self, *args: Any) -> _FakeQuery:
        return _FakeQuery(self.rows, self._compile(args))

    async def find_one(self, *args: Any) -> _FakeDoc | None:
        matcher = self._compile(args)
        for doc in self.rows.values():
            if matcher(doc):
                return doc
        return None

    @staticmethod
    def _compile(args: tuple[Any, ...]) -> Callable[[_FakeDoc], bool]:
        """Compile a list of (Beanie-style) match expressions into a callable.

        We accept the two forms used by the service:
          * raw dict like {"workspace": "ws-1", "starts_at": {"$lt": ...}}
          * comparison built from ``_EventDoc.id == oid`` / ``_EventDoc.workspace == "ws"``
            (these come through as a ``_FieldEq`` object the test installs).
        """

        def _match(doc: _FakeDoc) -> bool:
            for arg in args:
                if isinstance(arg, dict):
                    if not _dict_match(doc, arg):
                        return False
                elif isinstance(arg, _FieldEq):
                    if getattr(doc, arg.field) != arg.value:
                        return False
                else:
                    raise AssertionError(f"unhandled query arg: {arg!r}")
            return True

        return _match


def _dict_match(doc: _FakeDoc, filt: dict[str, Any]) -> bool:
    for key, value in filt.items():
        # Handle $or — at least one sub-condition must match.
        if key == "$or":
            if not any(_dict_match(doc, sub) for sub in value):
                return False
            continue

        actual = getattr(doc, key, None)
        if isinstance(value, dict):
            for op, operand in value.items():
                if op == "$lt" and not (actual is not None and actual < operand):
                    return False
                elif op == "$gt" and not (actual is not None and actual > operand):
                    return False
                elif op == "$ne":
                    # MongoDB $ne: null matches when the field value is not
                    # null (field is present with a truthy value).
                    if operand is None:
                        if actual is None:
                            return False
                    else:
                        if actual == operand:
                            return False
                elif op == "$in":
                    # only used for attendees.email which is nested — caller
                    # never asks for that path in service-level tests.
                    if actual not in operand:
                        return False
        else:
            if actual != value:
                return False
    return True


class _FakeQuery:
    def __init__(self, rows: dict[str, _FakeDoc], matcher) -> None:
        self.rows = rows
        self.matcher = matcher
        self._limit: int | None = None

    def limit(self, n: int) -> _FakeQuery:
        self._limit = n
        return self

    async def to_list(self) -> list[_FakeDoc]:
        out = [d for d in self.rows.values() if self.matcher(d)]
        if self._limit is not None:
            out = out[: self._limit]
        return out


class _FieldEq:
    """Stand-in for ``_EventDoc.workspace == 'x'``-style comparisons."""

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value


class _FieldProxy:
    """Lets tests/service write ``_EventDoc.workspace == 'x'`` against our fake."""

    def __init__(self, field: str) -> None:
        self._field = field

    def __eq__(self, other: Any) -> _FieldEq:  # type: ignore[override]
        return _FieldEq(self._field, other)


class _FakeDoc:
    def __init__(self, store: _FakeStore, **fields: Any) -> None:
        self._store = store
        self.id: ObjectId = fields.pop("_id", ObjectId())
        # Apply remaining fields.
        for k, v in fields.items():
            setattr(self, k, v)
        # Defaults to match the model.
        self.description = getattr(self, "description", "")
        self.location = getattr(self, "location", None)
        self.attendees = getattr(self, "attendees", [])
        self.recurrence = getattr(self, "recurrence", None)
        self.fabric_object_id = getattr(self, "fabric_object_id", None)
        self.source_connector = getattr(self, "source_connector", None)
        self.source_external_id = getattr(self, "source_external_id", None)
        self.created_at = getattr(self, "created_at", datetime.now(UTC))
        self.updated_at = getattr(self, "updated_at", datetime.now(UTC))
        # H-NEW-1: required on the real _EventDoc — default here to keep
        # legacy tests happy. The user id matches the default ctx() fixture
        # so creator-equality passes by default.
        self.created_by_user_id = getattr(self, "created_by_user_id", "user-test")

    async def insert(self) -> None:
        self._store.rows[str(self.id)] = self

    async def save(self) -> None:
        self._store.rows[str(self.id)] = self

    async def delete(self) -> None:
        self._store.rows.pop(str(self.id), None)


# ---------------------------------------------------------------------------
# Fixture: install the fake at the module the service imports.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_store(monkeypatch) -> _FakeStore:
    store = _FakeStore()
    # Patch the symbol service.py references. Service imports _EventDoc at
    # module-load time, so we replace it there.
    monkeypatch.setattr(service_module, "_EventDoc", store)
    # Service uses ``_EventDoc.id == oid`` and ``.workspace == ...`` via
    # ``_get_event_doc_or_404``. The fake _EventDoc is a class instance,
    # not a class — we attach proxies for the two fields service queries.
    store.id = _FieldProxy("id_str")  # type: ignore[attr-defined]
    store.workspace = _FieldProxy("workspace")  # type: ignore[attr-defined]
    return store


class _FakeCalendarStore:
    """Stand-in for ``_CalendarDoc``. By default returns ``None`` on every
    ``find_one`` so the service falls back to its synthetic default
    calendar (the bridge until Calendar CRUD ships). Individual tests can
    set ``.rows`` to override and exercise the private/shared paths."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def find_one(self, query: dict[str, Any]) -> Any | None:
        for row in self.rows:
            if all(getattr(row, k, None) == v for k, v in query.items() if not k.startswith("_")):
                return row
        return None


@pytest.fixture(autouse=True)
def fake_calendar_store(monkeypatch) -> _FakeCalendarStore:
    """Patch ``_CalendarDoc`` to return ``None`` on every lookup so the
    service exercises its synthetic-default fallback. Auto-applied to
    every test in this module so existing behaviour is preserved."""
    store = _FakeCalendarStore()
    monkeypatch.setattr(service_module, "_CalendarDoc", store)
    return store


def _new_doc(
    store: _FakeStore,
    *,
    workspace: str = "ws-test",
    calendar_id: str = "cal-1",
    title: str = "Meeting",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    attendees: list[dict] | None = None,
    created_by_user_id: str = "user-test",
) -> _FakeDoc:
    """Helper: stuff a doc into the fake store directly (skip service)."""
    oid = ObjectId()
    doc = _FakeDoc(
        store=store,
        _id=oid,
        workspace=workspace,
        calendar_id=calendar_id,
        title=title,
        starts_at=starts_at or datetime(2026, 5, 19, 9, 0),
        ends_at=ends_at or datetime(2026, 5, 19, 10, 0),
        timezone="UTC",
        description="",
        location=None,
        attendees=attendees or [],
        # H-NEW-1: defaults to the ctx() user so happy-path callers don't
        # have to know about this. Pass a different id to exercise the
        # non-creator denial path on update_event / delete_event.
        created_by_user_id=created_by_user_id,
    )
    # mirror the id as a string field so `_FieldEq("id_str", oid)` works.
    doc.id_str = oid
    store.rows[str(oid)] = doc
    return doc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_event_happy_path(ctx, fake_store, bus_spy):
    body = CreateEventRequest(
        calendar_id="cal-1",
        title="Team sync",
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 10, 0),
        timezone="UTC",
    )
    resp = await service_module.create_event(ctx, body)
    assert resp.title == "Team sync"
    assert resp.workspace_id == ctx.workspace_id
    assert resp.calendar_id == "cal-1"
    # Persisted in the fake store.
    assert len(fake_store.rows) == 1
    # Bus event emitted.
    topics = [t for t, _ in bus_spy]
    assert TOPIC_EVENT_CREATED in topics


def test_create_event_validates_input():
    """DTO rejects missing required title at construction."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        CreateEventRequest(
            calendar_id="cal-1",
            title="",  # min_length=1
            starts_at=datetime(2026, 5, 19, 9, 0),
            ends_at=datetime(2026, 5, 19, 10, 0),
            timezone="UTC",
        )


def test_create_event_rejects_end_before_start():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        CreateEventRequest(
            calendar_id="cal-1",
            title="x",
            starts_at=datetime(2026, 5, 19, 10, 0),
            ends_at=datetime(2026, 5, 19, 9, 0),  # before start
            timezone="UTC",
        )


async def test_list_events_tenant_filter(ctx, other_ctx, fake_store):
    """Cross-workspace reads return empty — never leak across tenants."""
    _new_doc(fake_store, workspace=ctx.workspace_id)
    _new_doc(fake_store, workspace=other_ctx.workspace_id)
    body = ListEventsRequest(
        starts_after=datetime(2026, 5, 1),
        starts_before=datetime(2026, 6, 1),
    )
    result = await service_module.list_events(ctx, body)
    assert result.total == 1
    assert result.events[0].workspace_id == ctx.workspace_id


async def test_update_event_happy_path(ctx, fake_store, bus_spy):
    doc = _new_doc(fake_store, workspace=ctx.workspace_id, title="Old title")
    body = UpdateEventRequest(title="New title")
    resp = await service_module.update_event(ctx, str(doc.id), body)
    assert resp.title == "New title"
    topics = [t for t, _ in bus_spy]
    assert TOPIC_EVENT_UPDATED in topics


async def test_update_event_not_found(ctx, fake_store):
    """A 404 surfaces as NotFound — not a generic CloudError."""
    with pytest.raises(NotFound):
        await service_module.update_event(
            ctx,
            str(ObjectId()),  # valid object id, just no row
            UpdateEventRequest(title="Anything"),
        )


async def test_update_event_invalid_window_raises_validation(ctx, fake_store):
    """Cross-field invariant — starts < ends — checked at the service after merge."""
    doc = _new_doc(
        fake_store,
        workspace=ctx.workspace_id,
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 10, 0),
    )
    # Push starts past ends without updating ends — service should refuse.
    body = UpdateEventRequest(starts_at=datetime(2026, 5, 19, 11, 0))
    with pytest.raises(ValidationError):
        await service_module.update_event(ctx, str(doc.id), body)


async def test_delete_event_emits_bus_event(ctx, fake_store, bus_spy):
    doc = _new_doc(fake_store, workspace=ctx.workspace_id)
    await service_module.delete_event(ctx, str(doc.id))
    assert str(doc.id) not in fake_store.rows
    topics = [t for t, _ in bus_spy]
    assert TOPIC_EVENT_DELETED in topics


async def test_get_event_cross_workspace_returns_404(ctx, other_ctx, fake_store):
    doc = _new_doc(fake_store, workspace=other_ctx.workspace_id)
    with pytest.raises(NotFound):
        await service_module.get_event(ctx, str(doc.id))


async def test_get_freebusy_multi_attendee(ctx, fake_store, monkeypatch):
    """Cover the freebusy path. compute_freebusy is patched because the
    fake store doesn't model the embedded-attendee $in filter that the
    real Mongo query uses. The H2 access-resolver is also patched so we
    bypass the unknown-attendee gate (separately covered in
    test_freebusy.py)."""
    from pocketpaw_ee.calendar import service as svc

    async def _fake_compute(
        workspace_id, attendee_emails, starts_at, ends_at, accessible_calendar_ids=None
    ):
        from pocketpaw_ee.calendar.domain import FreeBusy

        return [FreeBusy(attendee_email=e, busy_periods=[]) for e in attendee_emails]

    async def _fake_access(ctx, *, attendee_emails, starts_at, ends_at):
        # All emails resolve to the canonical "cal-1" — bypass the
        # unknown-attendee gate so this test focuses on the happy path.
        return {"cal-1"}

    monkeypatch.setattr(svc, "compute_freebusy", _fake_compute)
    monkeypatch.setattr(svc, "_accessible_calendar_ids_with_email_match", _fake_access)

    body = FreeBusyRequest(
        attendee_emails=["a@example.com", "b@example.com"],
        starts_at=datetime(2026, 5, 19),
        ends_at=datetime(2026, 5, 20),
    )
    resp = await svc.get_freebusy(ctx, body)
    assert {fb.attendee_email for fb in resp.freebusy} == {"a@example.com", "b@example.com"}


async def test_detect_conflicts_overlapping_events(ctx, fake_store, bus_spy, monkeypatch):
    """detect_conflicts emits TOPIC_CONFLICT_DETECTED when conflicts exist."""
    from pocketpaw_ee.calendar import service as svc

    target = _new_doc(
        fake_store,
        workspace=ctx.workspace_id,
        starts_at=datetime(2026, 5, 19, 9, 0),
        ends_at=datetime(2026, 5, 19, 10, 0),
        attendees=[{"email": "alice@example.com", "is_organizer": True}],
    )

    other = _new_doc(
        fake_store,
        workspace=ctx.workspace_id,
        starts_at=datetime(2026, 5, 19, 9, 30),
        ends_at=datetime(2026, 5, 19, 10, 30),
        attendees=[{"email": "alice@example.com", "is_organizer": False}],
    )

    async def _fake_find_conflicts(workspace_id, event):
        from pocketpaw_ee.calendar.conflicts import _doc_to_event

        # mirror real find_conflicts: just return `other` mapped.
        return [_doc_to_event(other)]

    monkeypatch.setattr(svc, "find_conflicts", _fake_find_conflicts)

    report = await svc.detect_conflicts(ctx, str(target.id))
    assert len(report.conflicting_events) == 1
    topics = [t for t, _ in bus_spy]
    assert TOPIC_CONFLICT_DETECTED in topics


# ---------------------------------------------------------------------------
# H-NEW-1: event-level modify authz on the synthetic-default Calendar path.
# ---------------------------------------------------------------------------
#
# The audit on #1143 surfaced that _load_calendar returns a synthetic Calendar
# with owner_user_id = ctx.user_id whenever no _CalendarDoc row exists. The
# autouse `fake_calendar_store` fixture in this module always returns None
# from find_one, so every test here exercises that synthetic-default path.
# These four tests assert that policy.check_event_modify closes the gap.


async def test_update_event_non_creator_denied_synthetic_calendar(fake_store, bus_spy):
    """Bob (same workspace, NOT the event creator) cannot update Alice's event
    even though the parent Calendar is synthetic-default."""
    from pocketpaw_ee.calendar._context import RequestContext
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    # Event was created by alice. Bob shares the workspace.
    doc = _new_doc(fake_store, workspace="ws-test", created_by_user_id="alice")
    bob_ctx = RequestContext(workspace_id="ws-test", user_id="bob")

    with pytest.raises(Forbidden) as exc_info:
        await service_module.update_event(
            bob_ctx,
            str(doc.id),
            UpdateEventRequest(title="Hijacked"),
        )
    assert "event.modify_denied" in str(exc_info.value)


async def test_delete_event_non_creator_denied_synthetic_calendar(fake_store, bus_spy):
    """Same shape as the update test but for delete_event — bob can't drop
    alice's event on the synthetic-default Calendar."""
    from pocketpaw_ee.calendar._context import RequestContext
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    doc = _new_doc(fake_store, workspace="ws-test", created_by_user_id="alice")
    bob_ctx = RequestContext(workspace_id="ws-test", user_id="bob")

    with pytest.raises(Forbidden) as exc_info:
        await service_module.delete_event(bob_ctx, str(doc.id))
    assert "event.modify_denied" in str(exc_info.value)
    # Event still in the store — delete must have aborted before .delete().
    assert str(doc.id) in fake_store.rows


async def test_update_event_creator_allowed_synthetic_calendar(ctx, fake_store, bus_spy):
    """Regression: the creator can still update their own event on a
    synthetic-default Calendar (happy path after the H-NEW-1 fix)."""
    # ctx fixture: user_id="user-test". Doc default creator matches.
    doc = _new_doc(fake_store, workspace=ctx.workspace_id, title="Original")
    resp = await service_module.update_event(
        ctx,
        str(doc.id),
        UpdateEventRequest(title="Edited by creator"),
    )
    assert resp.title == "Edited by creator"
    assert resp.created_by_user_id == "user-test"


async def test_create_event_synthetic_calendar_still_works(ctx, fake_store, bus_spy):
    """Regression: any workspace member can create events on a synthetic-
    default Calendar. The H-NEW-1 fix gates update/delete, not create."""
    body = CreateEventRequest(
        calendar_id="cal-1",
        title="Newcomer's first event",
        starts_at=datetime(2026, 5, 20, 14, 0),
        ends_at=datetime(2026, 5, 20, 15, 0),
        timezone="UTC",
    )
    resp = await service_module.create_event(ctx, body)
    # Creator stamped from ctx.user_id, never from the body.
    assert resp.created_by_user_id == ctx.user_id
    assert resp.title == "Newcomer's first event"


async def test_create_event_ignores_client_supplied_creator(ctx, fake_store, bus_spy):
    """The DTO doesn't accept created_by_user_id; even if a malicious client
    appends it, Pydantic's default model_config (extra=ignore) drops it and
    the service stamps ctx.user_id."""
    # Build the DTO with extra fields — Pydantic silently drops them by default.
    raw = {
        "calendar_id": "cal-1",
        "title": "Spoof attempt",
        "starts_at": datetime(2026, 5, 20, 14, 0),
        "ends_at": datetime(2026, 5, 20, 15, 0),
        "timezone": "UTC",
        # Attempt to override the creator. Should be ignored.
        "created_by_user_id": "someone-else",
    }
    body = CreateEventRequest.model_validate(raw)
    # Sanity: DTO doesn't carry the spoofed field on the way in.
    assert not hasattr(body, "created_by_user_id")

    resp = await service_module.create_event(ctx, body)
    # Service stamped its own ctx.user_id, not the spoofed value.
    assert resp.created_by_user_id == ctx.user_id
    assert resp.created_by_user_id != "someone-else"
