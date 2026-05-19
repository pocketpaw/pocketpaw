# Calendar module — Beanie document models.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H-NEW-1).
#
# Changes:
# - H-NEW-1: _EventDoc now persists created_by_user_id (no default). The
#   service writes ctx.user_id on insert; policy.check_event_modify reads
#   it on update/delete to gate authz on the synthetic-default calendar
#   path.
#
# Internal-only — never imported outside ee/calendar/. The single rule:
# nothing outside this module talks to the database. Callers go through
# service.py, which is the only place these docs are constructed or queried.
# Underscore prefix on the class name is a load-bearing signal — if you
# see _EventDoc in an import outside this module, that's a bug.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class _CalendarDoc(Document):
    """Beanie document for a Calendar. Workspace-scoped (workspace field,
    matching the ee/cloud convention of `workspace` not `workspace_id` at
    the DB layer)."""

    workspace: str
    name: str
    owner_user_id: str
    timezone: str
    color: str = "#0A84FF"
    visibility: str = "public_to_workspace"
    shared_with_user_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    class Settings:
        name = "calendars"
        indexes = [
            IndexModel([("workspace", ASCENDING)]),
            IndexModel([("workspace", ASCENDING), ("owner_user_id", ASCENDING)]),
        ]


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


class _EventDoc(Document):
    """Beanie document for a calendar event.

    `attendees`, `recurrence` stored as raw dicts/lists to avoid coupling the
    DB layer to Pydantic frozen models. Service.py maps to/from the domain
    types via model_validate / model_dump.

    `source_connector` + `source_external_id` are the reconciliation keys
    used by sync.py — if both are set, the event came from an external
    system and must be matched on its external id.
    """

    workspace: str
    calendar_id: str
    title: str
    description: str = ""
    starts_at: datetime
    ends_at: datetime
    timezone: str
    # H-NEW-1: required for event-level modify authz. Set from ctx.user_id
    # on insert. No default — old docs predating the field will surface as
    # a Pydantic validation error on read, which is the right behaviour
    # since H-NEW-1 shipped before any production data was written.
    created_by_user_id: str
    location: str | None = None
    attendees: list[dict[str, Any]] = Field(default_factory=list)
    recurrence: dict[str, Any] | None = None
    fabric_object_id: str | None = None
    source_connector: str | None = None
    source_external_id: str | None = None
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    class Settings:
        name = "calendar_events"
        indexes = [
            IndexModel(
                [
                    ("workspace", ASCENDING),
                    ("calendar_id", ASCENDING),
                    ("starts_at", ASCENDING),
                ]
            ),
            IndexModel([("workspace", ASCENDING), ("starts_at", ASCENDING)]),
            IndexModel([("source_connector", ASCENDING), ("source_external_id", ASCENDING)]),
            # H-NEW-1: support cheap "events I created" filters (future:
            # personal-view, modify-allowlist UI). Composite with workspace
            # to honour the tenant filter rule.
            IndexModel([("workspace", ASCENDING), ("created_by_user_id", ASCENDING)]),
        ]
