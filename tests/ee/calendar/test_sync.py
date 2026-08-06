# tests/ee/calendar/test_sync.py — external-sync reconciliation tests.
#
# Created: 2026-08-06 (feat/coupling-calendar-sot, T-13).
#
# Covers ``ingest_composio_events`` (the Composio → store reconciliation
# the agent preamble's refresh path calls) and the cross-connector dedupe
# guarantee: the same upstream Google event arriving via BOTH the native
# gcalendar pull and the Composio pull lands as exactly ONE ``_EventDoc``
# row, in either ingest order. Unlike test_service.py's FakeStore
# approach, these run against a real (mongomock) Beanie store because the
# thing under test IS the store reconciliation — faking find/insert here
# would mock the seam under test.
#
# Mutation gates (tests/mutations/calendar_sot.json):
#   * dropping the workspace filter from _find_google_event → the
#     tenancy test fails (ingest would update another workspace's row).
#   * narrowing the family matcher to composio-only → both dedupe
#     tests fail (a second row appears).

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pocketpaw_ee.calendar import sync
from pocketpaw_ee.calendar._context import RequestContext
from pocketpaw_ee.calendar.models import _CalendarDoc, _EventDoc

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def calendar_db() -> Any:
    """Beanie against a fresh mongomock DB — calendar documents only.

    Mirrors tests/cloud/conftest.py's ``mongo_db`` (including the
    ``list_collection_names`` kwargs shim) but stays local to the ee
    calendar suite so these tests don't depend on the cloud conftest.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    client = AsyncMongoMockClient()
    db = client[f"test_{uuid.uuid4().hex[:8]}"]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args: Any, **_kwargs: Any):
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[_CalendarDoc, _EventDoc])
    yield db


@pytest.fixture
def db_ctx() -> RequestContext:
    return RequestContext(workspace_id="ws-sync", user_id="user-sync")


def _google_item(
    *,
    id: str,
    summary: str = "Synced event",
    start: str | None = None,
    end: str | None = None,
    attendees: list[str] | None = None,
    all_day: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """One Google ``events.list`` row, as Composio hands it over."""
    start = start or (datetime.now(UTC) + timedelta(days=1)).isoformat()
    end = end or (datetime.now(UTC) + timedelta(days=1, hours=1)).isoformat()
    key = "date" if all_day else "dateTime"
    item: dict[str, Any] = {
        "id": id,
        "summary": summary,
        "start": {key: start},
        "end": {key: end},
        **extra,
    }
    if attendees is not None:
        item["attendees"] = [{"email": e} for e in attendees]
    return item


class _FakeGCalClient:
    """Stand-in for pocketpaw.clients.gcalendar.CalendarClient.

    The native pull's wire shape is flat ISO strings (not Google's nested
    start/end blocks) — mirror that.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def list_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self._events


def _native_event(*, id: str, summary: str = "Native event") -> dict[str, Any]:
    return {
        "id": id,
        "summary": summary,
        "start": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        "end": (datetime.now(UTC) + timedelta(days=2, hours=1)).isoformat(),
        "attendees": [],
    }


def _patch_native_client(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> None:
    import pocketpaw.clients.gcalendar as gcal_module

    monkeypatch.setattr(gcal_module, "CalendarClient", lambda: _FakeGCalClient(events))


# ---------------------------------------------------------------------------
# ingest_composio_events — creation + reconciliation
# ---------------------------------------------------------------------------


async def test_ingest_creates_rows_with_composio_connector(
    calendar_db: Any, db_ctx: RequestContext
) -> None:
    """A Composio-only event lands in the store with the reconciliation
    keys set — this is what makes it visible on /calendar."""
    touched = await sync.ingest_composio_events(
        db_ctx,
        [_google_item(id="gid-1", summary="Standup", attendees=["a@example.com"])],
    )
    assert touched == 1

    rows = await _EventDoc.find({"workspace": "ws-sync"}).to_list()
    assert len(rows) == 1
    row = rows[0]
    assert row.source_connector == "composio_google"
    assert row.source_external_id == "gid-1"
    assert row.title == "Standup"
    assert row.created_by_user_id == "user-sync"
    assert row.attendees == [
        {"email": "a@example.com", "response": "needs_action", "is_organizer": False}
    ]


async def test_ingest_lands_on_private_per_user_calendar(
    calendar_db: Any, db_ctx: RequestContext
) -> None:
    """The Composio connection is per-user, so its events are one
    member's personal feed. The ingest must auto-create a PRIVATE
    calendar owned by the syncing user and land rows on it — landing on
    the synthetic workspace-public default would expose member A's
    personal events to every other member."""
    await sync.ingest_composio_events(db_ctx, [_google_item(id="gid-1")])

    cals = await _CalendarDoc.find({"workspace": "ws-sync"}).to_list()
    assert len(cals) == 1
    cal = cals[0]
    assert cal.visibility == "private"
    assert cal.owner_user_id == "user-sync"

    row = await _EventDoc.find_one({"workspace": "ws-sync"})
    assert row is not None
    assert row.calendar_id == str(cal.id)
    assert row.calendar_id != "primary"


async def test_ingest_reuses_the_private_calendar(calendar_db: Any, db_ctx: RequestContext) -> None:
    """Repeat ingests (every TTL window, forever) must reuse the one
    private calendar, not mint a new doc per refresh."""
    await sync.ingest_composio_events(db_ctx, [_google_item(id="gid-1")])
    await sync.ingest_composio_events(db_ctx, [_google_item(id="gid-2")])

    cals = await _CalendarDoc.find({"workspace": "ws-sync"}).to_list()
    assert len(cals) == 1
    rows = await _EventDoc.find({"workspace": "ws-sync"}).to_list()
    assert {r.calendar_id for r in rows} == {str(cals[0].id)}


async def test_two_members_same_upstream_event_get_their_own_rows(
    calendar_db: Any,
) -> None:
    """Two members of ONE workspace both invited to the same Google
    meeting, both with Composio wired: each gets their own private row.
    Deduping across users would strand member B's copy on member A's
    private calendar, where B cannot see it."""
    ctx_a = RequestContext(workspace_id="ws-sync", user_id="member-a")
    ctx_b = RequestContext(workspace_id="ws-sync", user_id="member-b")

    await sync.ingest_composio_events(ctx_a, [_google_item(id="gid-shared", summary="Team sync")])
    await sync.ingest_composio_events(ctx_b, [_google_item(id="gid-shared", summary="Team sync")])

    rows = await _EventDoc.find(
        {"workspace": "ws-sync", "source_external_id": "gid-shared"}
    ).to_list()
    assert len(rows) == 2
    assert {r.created_by_user_id for r in rows} == {"member-a", "member-b"}
    # Each row sits on its owner's own private calendar.
    assert len({r.calendar_id for r in rows}) == 2
    cals = await _CalendarDoc.find({"workspace": "ws-sync"}).to_list()
    assert {c.owner_user_id for c in cals} == {"member-a", "member-b"}
    assert all(c.visibility == "private" for c in cals)


async def test_ingest_captures_google_timezone(calendar_db: Any, db_ctx: RequestContext) -> None:
    """Google's ``start.timeZone`` (IANA) is persisted so read paths can
    render event-local wall time; absent or garbage zones keep the UTC
    canonical stamp."""
    start = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)  # 10:30 IST
    items = [
        {
            "id": "gid-ist",
            "summary": "IST meeting",
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {
                "dateTime": (start + timedelta(hours=1)).isoformat(),
                "timeZone": "Asia/Kolkata",
            },
        },
        _google_item(id="gid-no-tz"),
        {
            "id": "gid-bad-tz",
            "summary": "Garbage zone",
            "start": {"dateTime": start.isoformat(), "timeZone": "Not/AZone"},
            "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        },
    ]
    await sync.ingest_composio_events(db_ctx, items)

    ist = await _EventDoc.find_one({"workspace": "ws-sync", "source_external_id": "gid-ist"})
    no_tz = await _EventDoc.find_one({"workspace": "ws-sync", "source_external_id": "gid-no-tz"})
    bad_tz = await _EventDoc.find_one({"workspace": "ws-sync", "source_external_id": "gid-bad-tz"})
    assert ist is not None and ist.timezone == "Asia/Kolkata"
    assert no_tz is not None and no_tz.timezone == "UTC"
    assert bad_tz is not None and bad_tz.timezone == "UTC"


async def test_ingest_twice_updates_in_place(calendar_db: Any, db_ctx: RequestContext) -> None:
    """Re-ingesting the same Google event id updates the one row —
    the preamble's sync-on-read must not grow the store per refresh."""
    await sync.ingest_composio_events(db_ctx, [_google_item(id="gid-1", summary="Before")])
    await sync.ingest_composio_events(db_ctx, [_google_item(id="gid-1", summary="After")])

    rows = await _EventDoc.find({"workspace": "ws-sync"}).to_list()
    assert len(rows) == 1
    assert rows[0].title == "After"


async def test_ingest_dedupes_against_native_gcalendar_row(
    calendar_db: Any, db_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native connector synced first; Composio pulls the SAME upstream
    event (both wired to one Google account). One row, and the native
    connector tag survives — provenance belongs to who ingested first."""
    _patch_native_client(monkeypatch, [_native_event(id="gid-shared", summary="Native title")])
    await sync.pull_from_gcalendar(db_ctx, calendar_id="primary")

    await sync.ingest_composio_events(
        db_ctx, [_google_item(id="gid-shared", summary="Composio title")]
    )

    rows = await _EventDoc.find(
        {"workspace": "ws-sync", "source_external_id": "gid-shared"}
    ).to_list()
    assert len(rows) == 1
    assert rows[0].source_connector == "gcalendar"
    # Composio's fresher payload still updated the fields.
    assert rows[0].title == "Composio title"


async def test_native_pull_dedupes_against_composio_row(
    calendar_db: Any, db_ctx: RequestContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse ingest order: Composio first, then the native gcalendar
    pull sees the same Google event id. Still one row."""
    await sync.ingest_composio_events(
        db_ctx, [_google_item(id="gid-shared", summary="Composio title")]
    )

    _patch_native_client(monkeypatch, [_native_event(id="gid-shared", summary="Native title")])
    touched = await sync.pull_from_gcalendar(db_ctx, calendar_id="primary")
    assert touched == 1

    rows = await _EventDoc.find(
        {"workspace": "ws-sync", "source_external_id": "gid-shared"}
    ).to_list()
    assert len(rows) == 1
    assert rows[0].source_connector == "composio_google"
    assert rows[0].title == "Native title"


# ---------------------------------------------------------------------------
# ingest_composio_events — input hygiene
# ---------------------------------------------------------------------------


async def test_ingest_skips_malformed_items(calendar_db: Any, db_ctx: RequestContext) -> None:
    """No id, unparseable time, or an inverted window → the row is
    skipped, never a partial insert."""
    good_start = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    good_end = (datetime.now(UTC) + timedelta(days=1, hours=1)).isoformat()
    items = [
        _google_item(id="ok-1"),
        {"summary": "no id", "start": {"dateTime": good_start}, "end": {"dateTime": good_end}},
        {"id": "", "summary": "empty id"},
        {"id": "bad-time", "start": {"dateTime": "not-a-date"}, "end": {"dateTime": good_end}},
        _google_item(id="inverted", start=good_end, end=good_start),
    ]
    touched = await sync.ingest_composio_events(db_ctx, items)
    assert touched == 1

    rows = await _EventDoc.find({"workspace": "ws-sync"}).to_list()
    assert [r.source_external_id for r in rows] == ["ok-1"]


async def test_ingest_handles_all_day_events(calendar_db: Any, db_ctx: RequestContext) -> None:
    """Google all-day events carry ``start.date`` / ``end.date`` —
    parsed as midnights, not skipped."""
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    day_after = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    touched = await sync.ingest_composio_events(
        db_ctx, [_google_item(id="allday-1", start=tomorrow, end=day_after, all_day=True)]
    )
    assert touched == 1
    row = await _EventDoc.find_one({"workspace": "ws-sync"})
    assert row is not None
    assert row.starts_at.date().isoformat() == tomorrow


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_ingest_is_workspace_scoped(calendar_db: Any) -> None:
    """The same Google event id in two workspaces is two rows — the
    reconciliation match includes the workspace, so one tenant's ingest
    can never update (or be deduped against) another tenant's row."""
    ctx_a = RequestContext(workspace_id="ws-a", user_id="user-a")
    ctx_b = RequestContext(workspace_id="ws-b", user_id="user-b")

    await sync.ingest_composio_events(ctx_a, [_google_item(id="gid-1", summary="A's copy")])
    await sync.ingest_composio_events(ctx_b, [_google_item(id="gid-1", summary="B's copy")])

    rows_a = await _EventDoc.find({"workspace": "ws-a"}).to_list()
    rows_b = await _EventDoc.find({"workspace": "ws-b"}).to_list()
    assert len(rows_a) == 1
    assert len(rows_b) == 1
    assert rows_a[0].title == "A's copy"
    assert rows_b[0].title == "B's copy"
