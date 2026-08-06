# tests/cloud/calendar/test_service.py — Cloud calendar service.
#
# Updated: 2026-08-06 (feat/coupling-calendar-sot, T-13) — rewritten for
# the projection architecture: ``list_upcoming`` now reads the canonical
# calendar store (``pocketpaw_ee.calendar``) and refreshes it from
# Composio at most once per TTL window, instead of returning raw
# Composio payloads. Tests run against a real (mongomock) Beanie store
# via the ``mongo_db`` fixture because the guarantee under test is
# store/preamble parity — faking the store would mock the seam.
#
# Guarantees:
#   1. Tenancy + bounds guards refuse bad input (unchanged from #1214).
#   2. PARITY — the agent preamble's event set equals what /calendar's
#      ``list_events`` returns: native, bridge-minted, and
#      Composio-synced events all appear in both.
#   3. A Composio-only event becomes visible on /calendar after the
#      sync-on-read ingest.
#   4. The same Google event arriving via BOTH connectors renders once.
#   5. Composio outage → the preamble still serves from the store
#      (degraded freshness, not a broken surface).
#   6. TTL — repeated preamble builds within the window hit Composio at
#      most once.
#   7. Cross-workspace isolation on the store read path.
#
# Composio is doubled at the service boundary (``is_enabled`` /
# ``_get_client`` / ``composio_user_id``) exactly as before — we test
# the calendar service's contract, not Composio's wire format.
#
# Mutation gates (tests/mutations/calendar_sot.json): skipping the store
# projection, dropping the workspace filter, breaking the source
# mapping, and removing the TTL check are each caught here.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.calendar._context import RequestContext as CalendarContext
from pocketpaw_ee.calendar.dto import CreateEventRequest, ListEventsRequest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.calendar import service as calendar_service
from pocketpaw_ee.cloud.composio.domain import ComposioUserId

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_refresh_cache():
    """The sync-on-read TTL memory is module-level state — reset it per
    test so one test's refresh doesn't suppress another's."""
    calendar_service._reset_refresh_cache()
    yield
    calendar_service._reset_refresh_cache()


@pytest.fixture(autouse=True)
def _silence_calendar_bus(monkeypatch: pytest.MonkeyPatch):
    """Seeding events via ``calendar.service.create_event`` emits on the
    shared event bus; if another test module registered the meetings
    bridge in this process, the fan-out would mint Meeting rows mid-test.
    Silence emission — bridge behaviour is tested in its own suite."""
    from pocketpaw_ee.cloud.shared.events import event_bus

    async def _no_emit(_topic: str, _data: dict) -> None:
        return None

    monkeypatch.setattr(event_bus, "emit", _no_emit)


def _patch_composio(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    execute_return: Any = None,
    execute_side_effect: Exception | None = None,
) -> MagicMock:
    """Wire the composio service surface to a controllable double.

    Returns the ``client.tools.execute`` mock so individual tests can
    assert on how many times it was called and with what arguments.
    """
    from pocketpaw_ee.cloud.composio import service as composio_service

    monkeypatch.setattr(composio_service, "is_enabled", lambda *a, **kw: enabled)

    namespaced = ComposioUserId(enterprise_id="ent_test", user_id="user_test")
    monkeypatch.setattr(
        composio_service,
        "composio_user_id",
        lambda ctx, settings=None: namespaced,
    )

    execute = MagicMock(name="tools.execute")
    if execute_side_effect is not None:
        execute.side_effect = execute_side_effect
    else:
        execute.return_value = execute_return

    client = MagicMock(name="composio_client")
    client.tools.execute = execute

    async def _fake_get_client(settings: Any = None) -> MagicMock:
        return client

    monkeypatch.setattr(composio_service, "_get_client", _fake_get_client)
    return execute


def _google_event(
    *,
    id: str,
    summary: str,
    start: datetime | None = None,
    end: datetime | None = None,
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    """Build a Google-Calendar-shaped ``items[]`` row for the mocked
    Composio response. Times default to tomorrow so the event lands in
    the projection's upcoming window."""
    start = start or (datetime.now(UTC) + timedelta(days=1))
    end = end or (start + timedelta(hours=1))
    item: dict[str, Any] = {
        "id": id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if attendees:
        item["attendees"] = [{"email": e} for e in attendees]
    return item


async def _seed_event(
    workspace_id: str,
    title: str,
    *,
    user_id: str = "user_test",
    calendar_id: str = "primary",
    days_ahead: float = 1.0,
    fabric_object_id: str | None = None,
) -> str:
    """Create a store event the way real writers do (the /calendar API
    and the meetings reverse-bridge both go through ``create_event``).
    Returns the canonical event id."""
    from pocketpaw_ee.calendar.service import create_event

    starts = datetime.now(UTC) + timedelta(days=days_ahead)
    resp = await create_event(
        CalendarContext(workspace_id=workspace_id, user_id=user_id),
        CreateEventRequest(
            calendar_id=calendar_id,
            title=title,
            starts_at=starts,
            ends_at=starts + timedelta(hours=1),
            timezone="UTC",
            fabric_object_id=fabric_object_id,
        ),
    )
    return resp.id


async def _calendar_page_ids(workspace_id: str, user_id: str = "user_test") -> set[str]:
    """The /calendar page's view of the upcoming window — the parity
    oracle. Same store API the calendar router serves."""
    from pocketpaw_ee.calendar.service import list_events

    now = datetime.now(UTC).replace(tzinfo=None)
    listed = await list_events(
        CalendarContext(workspace_id=workspace_id, user_id=user_id),
        ListEventsRequest(starts_after=now, starts_before=now + timedelta(days=30)),
    )
    return {ev.id for ev in listed.events}


# ---------------------------------------------------------------------------
# Tenancy + bounds guards
# ---------------------------------------------------------------------------


async def test_empty_workspace_id_raises_validation_error() -> None:
    """The first cloud entity rule is "domain enforces tenancy at
    construction". The service mirrors it: empty workspace_id is a
    refusal, not a quiet degrade."""
    with pytest.raises(ValidationError, match="workspace_required"):
        await calendar_service.list_upcoming("", "user_test", limit=5)


async def test_empty_user_id_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="user_required"):
        await calendar_service.list_upcoming("ws_acme", "", limit=5)


async def test_non_positive_limit_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="invalid_limit"):
        await calendar_service.list_upcoming("ws_acme", "user_test", limit=0)


# ---------------------------------------------------------------------------
# Projection — the store is the source of truth
# ---------------------------------------------------------------------------


async def test_serves_store_events_when_composio_disabled(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """The old path returned ``[]`` whenever Composio was off — hiding
    every native and bridge-minted event from the agent. The projection
    serves the store regardless of Composio."""
    execute = _patch_composio(monkeypatch, enabled=False)
    event_id = await _seed_event("ws_acme", "Native standup")

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)

    assert [ev["id"] for ev in out] == [event_id]
    assert out[0]["title"] == "Native standup"
    assert out[0]["workspace_id"] == "ws_acme"
    assert out[0]["source"] == "local"
    execute.assert_not_called()


async def test_preamble_parity_with_calendar_page(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """THE T-13 invariant: the agent's "your upcoming events" and the
    /calendar page list the same set — native, bridge-minted, and
    Composio-synced events all present in both."""
    native_id = await _seed_event("ws_acme", "Native standup")
    bridge_id = await _seed_event(
        "ws_acme",
        "Client call",
        calendar_id="meetings",
        fabric_object_id="meeting:m1",
        days_ahead=2.0,
    )
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={
            "data": {"items": [_google_event(id="gid-1", summary="Composio sync")]},
            "successful": True,
        },
    )

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=10)
    preamble_ids = {ev["id"] for ev in out}

    page_ids = await _calendar_page_ids("ws_acme")
    assert preamble_ids == page_ids
    assert {native_id, bridge_id} <= preamble_ids
    assert len(preamble_ids) == 3
    # Bridge-minted and native events carry the "local" source; the
    # Composio-synced one maps to "google".
    by_id = {ev["id"]: ev for ev in out}
    assert by_id[native_id]["source"] == "local"
    assert by_id[bridge_id]["source"] == "local"
    composio_ev = next(ev for ev in out if ev["title"] == "Composio sync")
    assert composio_ev["source"] == "google"


async def test_composio_only_event_appears_on_calendar_after_ingest(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """The reverse half of the old split-brain: a Composio-only event
    used to be invisible on /calendar. The sync-on-read ingest lands it
    in the store, where the page's ``list_events`` sees it."""
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={
            "data": {"items": [_google_event(id="gid-only", summary="Composio-only")]},
            "successful": True,
        },
    )

    assert await _calendar_page_ids("ws_acme") == set()

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert [ev["title"] for ev in out] == ["Composio-only"]

    from pocketpaw_ee.calendar.service import list_events

    now = datetime.now(UTC).replace(tzinfo=None)
    listed = await list_events(
        CalendarContext(workspace_id="ws_acme", user_id="user_test"),
        ListEventsRequest(starts_after=now, starts_before=now + timedelta(days=30)),
    )
    assert [ev.title for ev in listed.events] == ["Composio-only"]
    assert listed.events[0].source_connector == "composio_google"
    assert listed.events[0].source_external_id == "gid-only"


async def test_same_google_event_via_both_connectors_renders_once(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """A user with the native gcalendar connector AND Composio wired to
    one Google account: the shared upstream event must not double."""
    from pocketpaw_ee.calendar import sync

    # Native pull ingests the upstream event first.
    class _FakeGCalClient:
        async def list_events(self, **_kw: Any) -> list[dict[str, Any]]:
            starts = datetime.now(UTC) + timedelta(days=1)
            return [
                {
                    "id": "gid-shared",
                    "summary": "Shared upstream event",
                    "start": starts.isoformat(),
                    "end": (starts + timedelta(hours=1)).isoformat(),
                    "attendees": [],
                }
            ]

    import pocketpaw.clients.gcalendar as gcal_module

    monkeypatch.setattr(gcal_module, "CalendarClient", lambda: _FakeGCalClient())
    await sync.pull_from_gcalendar(
        CalendarContext(workspace_id="ws_acme", user_id="user_test"), "primary"
    )

    # Composio then returns the SAME Google event id.
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={
            "data": {"items": [_google_event(id="gid-shared", summary="Shared upstream event")]},
            "successful": True,
        },
    )

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=10)
    assert len(out) == 1
    assert out[0]["source"] == "google"
    assert await _calendar_page_ids("ws_acme") == {out[0]["id"]}


# ---------------------------------------------------------------------------
# Degradation + freshness
# ---------------------------------------------------------------------------


async def test_composio_outage_still_serves_store(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """Upstream 5xx / network error / "no connected account": the old
    path returned ``[]``; the projection serves what the store has —
    degraded freshness, not a broken preamble."""
    event_id = await _seed_event("ws_acme", "Survives the outage")
    _patch_composio(
        monkeypatch,
        enabled=True,
        execute_side_effect=RuntimeError("composio: upstream 502"),
    )

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert [ev["id"] for ev in out] == [event_id]


async def test_refresh_is_ttl_gated(monkeypatch: pytest.MonkeyPatch, mongo_db: Any) -> None:
    """Sync-on-read hits Composio at most once per TTL window — repeat
    preamble builds within the window read the store only."""
    execute = _patch_composio(
        monkeypatch,
        enabled=True,
        execute_return={
            "data": {"items": [_google_event(id="gid-1", summary="Synced")]},
            "successful": True,
        },
    )

    await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert execute.call_count == 1

    # A new TTL window (simulated via the test hook) refreshes again.
    calendar_service._reset_refresh_cache()
    await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert execute.call_count == 2


async def test_store_projection_failure_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the store itself is unavailable (no Beanie init in this deploy
    shape), the service keeps its never-raise contract."""
    _patch_composio(monkeypatch, enabled=False)

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("store not initialized")

    monkeypatch.setattr(calendar_service, "_upcoming_from_store", _boom)
    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=5)
    assert out == []


# ---------------------------------------------------------------------------
# Tenancy + limit on the read path
# ---------------------------------------------------------------------------


async def test_workspaces_see_only_their_own_events(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """Store rows are tenant-filtered and every wire dict carries the
    requesting workspace's tag."""
    _patch_composio(monkeypatch, enabled=False)
    id_a = await _seed_event("ws_a", "A's event", user_id="user_a")
    id_b = await _seed_event("ws_b", "B's event", user_id="user_b")

    out_a = await calendar_service.list_upcoming("ws_a", "user_a", limit=10)
    out_b = await calendar_service.list_upcoming("ws_b", "user_b", limit=10)

    assert [ev["id"] for ev in out_a] == [id_a]
    assert [ev["id"] for ev in out_b] == [id_b]
    assert all(ev["workspace_id"] == "ws_a" for ev in out_a)
    assert all(ev["workspace_id"] == "ws_b" for ev in out_b)


async def test_limit_caps_results_sorted_by_start(
    monkeypatch: pytest.MonkeyPatch, mongo_db: Any
) -> None:
    """More store rows than ``limit`` → the soonest ``limit`` events, in
    start order."""
    _patch_composio(monkeypatch, enabled=False)
    ids = []
    for i in range(5):
        ids.append(await _seed_event("ws_acme", f"Event {i}", days_ahead=1.0 + i))

    out = await calendar_service.list_upcoming("ws_acme", "user_test", limit=3)
    assert [ev["id"] for ev in out] == ids[:3]
    starts = [ev["start"] for ev in out]
    assert starts == sorted(starts)
