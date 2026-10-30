# tests/ee/calendar/test_freebusy.py — free/busy availability tests.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H2).
#
# Changes:
# - Added test_freebusy_respects_accessible_calendar_ids: when the caller
#   supplies the H2 access-set, events on other calendars must NOT
#   contribute to the returned busy windows.
# - Added test_freebusy_empty_accessible_set_returns_no_busy: an empty
#   set means "no calendars I can read" — the response should reflect
#   that.
# - Added test_freebusy_none_accessible_preserves_legacy_behaviour: None
#   means "no enforcement" — the legacy callers (trusted internal use)
#   still get every busy block, no filtering.
#
# compute_freebusy queries _EventDoc.find with a $in on a nested attendees
# field. The fake store in test_service.py models that, but here we keep
# the test isolated: we patch _EventDoc.find directly at the freebusy
# module level to return a hand-built list.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.calendar import freebusy as fb_module


class _FakeDoc:
    """Lightweight stand-in for a Beanie _EventDoc row."""

    def __init__(
        self,
        starts_at: datetime,
        ends_at: datetime,
        attendees: list[dict[str, Any]],
        calendar_id: str = "cal-1",
    ) -> None:
        self.starts_at = starts_at
        self.ends_at = ends_at
        self.attendees = attendees
        self.calendar_id = calendar_id


class _FakeQuery:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs

    async def to_list(self) -> list[_FakeDoc]:
        return self._docs


class _FakeStore:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs
        self.last_query: dict[str, Any] | None = None

    def find(self, query: dict[str, Any]) -> _FakeQuery:
        self.last_query = query
        return _FakeQuery(self._docs)


@pytest.fixture
def patch_store(monkeypatch):
    """Install a fake _EventDoc.find for these tests only."""

    def _install(docs: list[_FakeDoc]) -> _FakeStore:
        store = _FakeStore(docs)
        monkeypatch.setattr(fb_module, "_EventDoc", store)
        return store

    return _install


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_freebusy_single_attendee_busy(patch_store):
    docs = [
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 10, 0),
            ends_at=datetime(2026, 5, 19, 11, 0),
            attendees=[{"email": "alice@example.com"}],
        ),
    ]
    patch_store(docs)

    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["alice@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
    )
    assert len(result) == 1
    assert result[0].attendee_email == "alice@example.com"
    assert result[0].busy_periods == [
        (datetime(2026, 5, 19, 10, 0), datetime(2026, 5, 19, 11, 0)),
    ]


async def test_freebusy_multi_attendee_overlap(patch_store):
    """Two events, two attendees — each shows only their own busy period."""
    docs = [
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 10, 0),
            ends_at=datetime(2026, 5, 19, 11, 0),
            attendees=[{"email": "alice@example.com"}],
        ),
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 14, 0),
            ends_at=datetime(2026, 5, 19, 15, 0),
            attendees=[{"email": "bob@example.com"}],
        ),
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 16, 0),
            ends_at=datetime(2026, 5, 19, 17, 0),
            attendees=[
                {"email": "alice@example.com"},
                {"email": "bob@example.com"},
            ],
        ),
    ]
    patch_store(docs)

    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["alice@example.com", "bob@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
    )
    by_email = {fb.attendee_email: fb.busy_periods for fb in result}
    assert len(by_email["alice@example.com"]) == 2
    assert len(by_email["bob@example.com"]) == 2


async def test_freebusy_no_events_returns_empty(patch_store):
    patch_store([])
    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["nobody@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
    )
    assert len(result) == 1
    assert result[0].busy_periods == []


async def test_freebusy_empty_emails_returns_empty():
    """Edge: no emails means we never even query the store."""
    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=[],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
    )
    assert result == []


async def test_freebusy_period_clipped_to_window(patch_store):
    """An event spanning the window boundary should be clipped on output."""
    docs = [
        _FakeDoc(
            starts_at=datetime(2026, 5, 18, 22, 0),
            ends_at=datetime(2026, 5, 19, 2, 0),
            attendees=[{"email": "alice@example.com"}],
        ),
    ]
    patch_store(docs)

    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["alice@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
    )
    period = result[0].busy_periods[0]
    assert period[0] == datetime(2026, 5, 19, 0, 0)
    assert period[1] == datetime(2026, 5, 19, 2, 0)


def test_freebusy_module_imports_clean():
    """Smoke: module-level constants and __all__ are consistent."""
    assert callable(fb_module.compute_freebusy)
    # UTC import is used inside module — make sure datetime UTC is still importable.
    assert UTC is not None


# ---------------------------------------------------------------------------
# H2 — accessible_calendar_ids restriction tests.
# ---------------------------------------------------------------------------


async def test_freebusy_respects_accessible_calendar_ids(patch_store):
    """Two events for the same attendee, only one on an accessible calendar.

    With ``accessible_calendar_ids`` restricting to that one calendar, only
    that event's window contributes to the result — the other is silently
    dropped (it would have leaked the private calendar's busy window
    otherwise — the H2 oracle).
    """
    docs = [
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 10, 0),
            ends_at=datetime(2026, 5, 19, 11, 0),
            attendees=[{"email": "alice@example.com"}],
            calendar_id="cal-public",
        ),
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 14, 0),
            ends_at=datetime(2026, 5, 19, 15, 0),
            attendees=[{"email": "alice@example.com"}],
            calendar_id="cal-private",
        ),
    ]
    patch_store(docs)

    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["alice@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
        accessible_calendar_ids={"cal-public"},
    )
    assert len(result) == 1
    assert result[0].busy_periods == [
        (datetime(2026, 5, 19, 10, 0), datetime(2026, 5, 19, 11, 0)),
    ]


async def test_freebusy_empty_accessible_set_returns_no_busy(patch_store):
    """Empty accessible set means the caller has no calendars they can
    read — the response is an empty-busy list for every requested email."""
    docs = [
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 10, 0),
            ends_at=datetime(2026, 5, 19, 11, 0),
            attendees=[{"email": "alice@example.com"}],
            calendar_id="cal-private",
        ),
    ]
    patch_store(docs)

    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["alice@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
        accessible_calendar_ids=set(),
    )
    assert len(result) == 1
    assert result[0].busy_periods == []


async def test_freebusy_none_accessible_preserves_legacy_behaviour(patch_store):
    """``accessible_calendar_ids=None`` (default) keeps the pre-H2 path so
    trusted internal callers still see every busy block."""
    docs = [
        _FakeDoc(
            starts_at=datetime(2026, 5, 19, 10, 0),
            ends_at=datetime(2026, 5, 19, 11, 0),
            attendees=[{"email": "alice@example.com"}],
            calendar_id="cal-private",
        ),
    ]
    patch_store(docs)

    result = await fb_module.compute_freebusy(
        workspace_id="ws-1",
        attendee_emails=["alice@example.com"],
        starts_at=datetime(2026, 5, 19, 0, 0),
        ends_at=datetime(2026, 5, 19, 23, 59),
        # accessible_calendar_ids defaults to None
    )
    assert len(result) == 1
    assert len(result[0].busy_periods) == 1
