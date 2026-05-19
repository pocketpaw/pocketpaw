# Calendar module — conflict detection.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H-NEW-1).
#
# Changes:
# - _doc_to_event now propagates created_by_user_id from the Beanie doc
#   into the domain Event. Required for policy.check_event_modify on
#   update/delete paths.
#
# Finds other events in the same workspace that overlap a given event's
# window and share at least one attendee. Excludes the event itself.
# Returns mapped Event domain objects (DB → domain via _doc_to_event).

from __future__ import annotations

from ee.calendar.domain import Attendee, Event, Recurrence
from ee.calendar.models import _EventDoc


def _doc_to_event(doc: _EventDoc) -> Event:
    """Map a Beanie document into the frozen Event domain value object."""
    attendees = [Attendee.model_validate(a) for a in (doc.attendees or [])]
    recurrence = Recurrence.model_validate(doc.recurrence) if doc.recurrence else None
    return Event(
        id=str(doc.id),
        workspace_id=doc.workspace,
        calendar_id=doc.calendar_id,
        title=doc.title,
        description=doc.description or "",
        starts_at=doc.starts_at,
        ends_at=doc.ends_at,
        timezone=doc.timezone,
        # H-NEW-1: hydrate the creator field so check_event_modify works
        # on Events returned by conflict scans (and any other domain
        # transit through this helper).
        created_by_user_id=doc.created_by_user_id,
        location=doc.location,
        attendees=attendees,
        recurrence=recurrence,
        fabric_object_id=doc.fabric_object_id,
        source_connector=doc.source_connector,
        source_external_id=doc.source_external_id,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def find_conflicts(workspace_id: str, event: Event) -> list[Event]:
    """Find other events overlapping `event`'s window with shared attendees.

    Two events overlap when start_a < end_b AND start_b < end_a. We push that
    filter into Mongo so we don't pull the whole workspace into memory. The
    shared-attendee filter runs in Python because attendees are stored as an
    embedded list of dicts.
    """
    if not event.attendees:
        return []

    overlapping_docs = await _EventDoc.find(
        {
            "workspace": workspace_id,
            "starts_at": {"$lt": event.ends_at},
            "ends_at": {"$gt": event.starts_at},
        }
    ).to_list()

    target_emails = {a.email.lower() for a in event.attendees}
    conflicts: list[Event] = []
    for doc in overlapping_docs:
        # Skip the event itself.
        if str(doc.id) == event.id:
            continue
        other_emails = {(a.get("email") or "").lower() for a in (doc.attendees or [])}
        if target_emails & other_emails:
            conflicts.append(_doc_to_event(doc))

    return conflicts
