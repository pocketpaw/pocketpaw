# tests/ee/calendar/test_service.py — service-layer tests.
# Created: 2026-05-19 (feat/calendar-module).
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

from ee.calendar import service as service_module
from ee.calendar.dto import (
    CreateEventRequest,
    FreeBusyRequest,
    ListEventsRequest,
    UpdateEventRequest,
)
from ee.calendar.events import (
    TOPIC_CONFLICT_DETECTED,
    TOPIC_EVENT_CREATED,
    TOPIC_EVENT_DELETED,
    TOPIC_EVENT_UPDATED,
)
from ee.cloud.shared.errors import NotFound, ValidationError

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
        actual = getattr(doc, key, None)
        if isinstance(value, dict):
            for op, operand in value.items():
                if op == "$lt" and not (actual is not None and actual < operand):
                    return False
                elif op == "$gt" and not (actual is not None and actual > operand):
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


def _new_doc(
    store: _FakeStore,
    *,
    workspace: str = "ws-test",
    calendar_id: str = "cal-1",
    title: str = "Meeting",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    attendees: list[dict] | None = None,
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
    real Mongo query uses."""
    from ee.calendar import service as svc

    async def _fake_compute(workspace_id, attendee_emails, starts_at, ends_at):
        from ee.calendar.domain import FreeBusy

        return [FreeBusy(attendee_email=e, busy_periods=[]) for e in attendee_emails]

    monkeypatch.setattr(svc, "compute_freebusy", _fake_compute)

    body = FreeBusyRequest(
        attendee_emails=["a@example.com", "b@example.com"],
        starts_at=datetime(2026, 5, 19),
        ends_at=datetime(2026, 5, 20),
    )
    resp = await svc.get_freebusy(ctx, body)
    assert {fb.attendee_email for fb in resp.freebusy} == {"a@example.com", "b@example.com"}


async def test_detect_conflicts_overlapping_events(ctx, fake_store, bus_spy, monkeypatch):
    """detect_conflicts emits TOPIC_CONFLICT_DETECTED when conflicts exist."""
    from ee.calendar import service as svc

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
        from ee.calendar.conflicts import _doc_to_event

        # mirror real find_conflicts: just return `other` mapped.
        return [_doc_to_event(other)]

    monkeypatch.setattr(svc, "find_conflicts", _fake_find_conflicts)

    report = await svc.detect_conflicts(ctx, str(target.id))
    assert len(report.conflicting_events) == 1
    topics = [t for t, _ in bus_spy]
    assert TOPIC_CONFLICT_DETECTED in topics
