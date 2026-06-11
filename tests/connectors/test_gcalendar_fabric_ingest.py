# test_gcalendar_fabric_ingest.py
# Created: 2026-06-11 (gap1-connfabric slice).
#
# Proves the connector->Fabric ingestion slice end to end:
#   GoogleCalendarConnector.ingest_to_fabric() pulls (mocked) calendar events
#   and lands them as typed `CalendarEvent` Fabric objects in a real FabricStore
#   with `source_connector`/`source_id` provenance — and a re-ingest UPDATES the
#   same objects rather than duplicating (idempotency via source_id).
#
# Also exercises the reusable mapper (FabricMapping / ingest_records) directly,
# independent of the calendar connector, since that is the pattern other
# connectors copy. No real HTTP / OAuth — CalendarClient.list_events is patched.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.connectors.adapters.gcalendar import (
    CALENDAR_EVENT_MAPPING,
    GoogleCalendarConnector,
)
from pocketpaw.connectors.fabric_ingest import FabricMapping, ingest_records
from pocketpaw.fabric.models import FabricQuery, PropertyDef
from pocketpaw.fabric.store import FabricStore

_CLIENT_LIST = "pocketpaw.clients.gcalendar.CalendarClient.list_events"


def _events(*ids_and_summaries: tuple[str, str]) -> list[dict]:
    """Build calendar-event records shaped like CalendarClient.list_events output."""
    out = []
    for ev_id, summary in ids_and_summaries:
        out.append(
            {
                "id": ev_id,
                "summary": summary,
                "start": "2026-06-12T10:00:00Z",
                "end": "2026-06-12T11:00:00Z",
                "location": "Room 1",
                "description": "",
                "attendees": ["a@x.com", "b@x.com"],
                "htmlLink": f"https://cal/{ev_id}",
            }
        )
    return out


# ---------------------------------------------------------------------------
# The connector slice: gcalendar -> typed Fabric objects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_lands_typed_objects_with_provenance(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    connector = GoogleCalendarConnector()

    events = _events(("evt_1", "Standup"), ("evt_2", "Q2 Review"))
    with patch(_CLIENT_LIST, new=AsyncMock(return_value=events)):
        result = await connector.ingest_to_fabric(store)

    # Ingest summary
    assert result.created == 2
    assert result.updated == 0
    assert result.skipped == 0
    assert result.type_name == "CalendarEvent"

    # A typed CalendarEvent ObjectType was defined
    obj_type = await store.get_type_by_name("CalendarEvent")
    assert obj_type is not None

    # Two typed objects landed, queryable by type
    q = await store.query(FabricQuery(type_name="CalendarEvent"))
    assert q.total == 2

    by_source = {o.source_id: o for o in q.objects}
    assert set(by_source) == {"evt_1", "evt_2"}

    standup = by_source["evt_1"]
    assert standup.type_id == obj_type.id
    assert standup.type_name == "CalendarEvent"
    # Provenance stamped
    assert standup.source_connector == "gcalendar"
    assert standup.source_id == "evt_1"
    # Field projection: declared properties present + derived attendee_count
    assert standup.properties["summary"] == "Standup"
    assert standup.properties["location"] == "Room 1"
    assert standup.properties["attendee_count"] == 2
    assert standup.properties["attendees"] == ["a@x.com", "b@x.com"]
    assert standup.properties["html_link"] == "https://cal/evt_1"


@pytest.mark.asyncio
async def test_reingest_is_idempotent_and_updates(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    connector = GoogleCalendarConnector()

    first = _events(("evt_1", "Standup"), ("evt_2", "Q2 Review"))
    with patch(_CLIENT_LIST, new=AsyncMock(return_value=first)):
        r1 = await connector.ingest_to_fabric(store)
    assert r1.created == 2

    # Re-sync: evt_1 retitled, evt_2 unchanged. Same source ids.
    second = _events(("evt_1", "Standup (moved)"), ("evt_2", "Q2 Review"))
    with patch(_CLIENT_LIST, new=AsyncMock(return_value=second)):
        r2 = await connector.ingest_to_fabric(store)

    # No duplicates: both records resolved to existing objects -> updates only
    assert r2.created == 0
    assert r2.updated == 2

    q = await store.query(FabricQuery(type_name="CalendarEvent"))
    assert q.total == 2  # still 2 objects, not 4

    # Object ids stable across syncs
    assert set(r1.object_ids) == set(r2.object_ids)

    # The updated property is reflected
    evt1 = await store.get_object_by_source("gcalendar", "evt_1")
    assert evt1 is not None
    assert evt1.properties["summary"] == "Standup (moved)"

    # Exactly one type was defined despite two syncs
    types = await store.list_types()
    assert sum(1 for t in types if t.name == "CalendarEvent") == 1


@pytest.mark.asyncio
async def test_records_without_source_id_are_skipped(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    connector = GoogleCalendarConnector()

    events = _events(("evt_1", "Standup"))
    events.append({"id": "", "summary": "no-id event", "attendees": []})  # blank id -> skip
    with patch(_CLIENT_LIST, new=AsyncMock(return_value=events)):
        result = await connector.ingest_to_fabric(store)

    assert result.created == 1
    assert result.skipped == 1
    q = await store.query(FabricQuery(type_name="CalendarEvent"))
    assert q.total == 1


# ---------------------------------------------------------------------------
# The reusable mapper, independent of any one connector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_records_pattern_is_connector_agnostic(tmp_path: Path) -> None:
    """ingest_records works for any connector that declares a FabricMapping."""
    store = FabricStore(tmp_path / "fabric.db")

    mapping = FabricMapping(
        type_name="Ticket",
        source_id_field="ticket_id",
        properties=[
            PropertyDef(name="title", type="string"),
            PropertyDef(name="priority", type="number"),
        ],
        field_map={
            "title": "subject",
            "priority": lambda r: int(r.get("priority", 0)),
        },
    )
    records = [
        {"ticket_id": "T-1", "subject": "Login broken", "priority": "3"},
        {"ticket_id": "T-2", "subject": "Typo", "priority": "1"},
    ]

    result = await ingest_records(store, connector="helpdesk", records=records, mapping=mapping)
    assert result.created == 2

    obj = await store.get_object_by_source("helpdesk", "T-1")
    assert obj is not None
    assert obj.source_connector == "helpdesk"
    assert obj.type_name == "Ticket"
    assert obj.properties["title"] == "Login broken"
    assert obj.properties["priority"] == 3  # derived/normalized via callable

    # Re-ingest with a changed value -> update, no duplicate
    records[0]["subject"] = "Login broken (P1)"
    again = await ingest_records(store, connector="helpdesk", records=records, mapping=mapping)
    assert again.updated == 2
    assert again.created == 0
    refreshed = await store.get_object_by_source("helpdesk", "T-1")
    assert refreshed is not None
    assert refreshed.properties["title"] == "Login broken (P1)"


@pytest.mark.asyncio
async def test_existing_type_is_reused_not_duplicated(tmp_path: Path) -> None:
    """If the ObjectType already exists, ingest reuses it (define-once-by-name)."""
    store = FabricStore(tmp_path / "fabric.db")
    # Pre-define the type the calendar mapping wants.
    predefined = await store.define_type(name="CalendarEvent", properties=[])

    events = _events(("evt_1", "Standup"))
    connector = GoogleCalendarConnector()
    with patch(_CLIENT_LIST, new=AsyncMock(return_value=events)):
        await connector.ingest_to_fabric(store)

    obj = await store.get_object_by_source("gcalendar", "evt_1")
    assert obj is not None
    # Object bound to the pre-existing type id, not a freshly-created duplicate
    assert obj.type_id == predefined.id
    types = [t for t in await store.list_types() if t.name == "CalendarEvent"]
    assert len(types) == 1


def test_calendar_mapping_declares_event_id_as_source() -> None:
    """The declared mapping pins the idempotency key + type name (snapshot)."""
    assert CALENDAR_EVENT_MAPPING.type_name == "CalendarEvent"
    assert CALENDAR_EVENT_MAPPING.source_id_field == "id"
    # source-id extraction + projection behave as the ingester expects
    rec = {"id": "  evt_x  ", "summary": "Hi", "attendees": ["a@x.com"]}
    assert CALENDAR_EVENT_MAPPING.extract_source_id(rec) == "evt_x"
    props = CALENDAR_EVENT_MAPPING.project(rec)
    assert props["summary"] == "Hi"
    assert props["attendee_count"] == 1
