# Calendar module — domain value objects.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 M1-M3 + H-NEW-1).
#
# Changes:
# - Attendee.email now uses EmailStr (M3) — Pydantic's email-validator
#   integration. Rejects malformed emails at the boundary instead of
#   propagating them into the DB.
# - Recurrence.rrule capped at 2048 chars (M1) — guards against
#   pathological RRULE strings that could choke recurrence expansion.
# - Recurrence.exceptions capped at 500 entries (M2) — guards against
#   unbounded growth that would slow down conflict detection.
# - H-NEW-1: Event now requires created_by_user_id (no default). The
#   service sets it from ctx.user_id on create_event. policy.
#   check_event_modify gates update/delete on creator-equality so a
#   synthetic-default Calendar doesn't grant cross-user modify access.
#
# Frozen Pydantic models representing the public calendar domain.
# workspace_id is required on Event and Calendar — enforced at construction
# time, no defaults — so multi-tenancy is impossible to forget.
# Mirrors the ee/cloud canonical "domain stays at the boundary" pattern.

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CalendarVisibility(StrEnum):
    PUBLIC_TO_WORKSPACE = "public_to_workspace"
    PRIVATE = "private"
    SHARED_WITH_USERS = "shared_with_users"


class AttendeeResponse(StrEnum):
    ACCEPTED = "accepted"
    DECLINED = "declined"
    TENTATIVE = "tentative"
    NEEDS_ACTION = "needs_action"


class ConflictSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class Attendee(BaseModel):
    """A participant on an event. user_id is optional — external (email-only)
    invitees are valid."""

    model_config = ConfigDict(frozen=True)

    email: EmailStr
    user_id: str | None = None
    name: str | None = None
    response: AttendeeResponse = AttendeeResponse.NEEDS_ACTION
    is_organizer: bool = False


class Recurrence(BaseModel):
    """RFC 5545 RRULE plus terminators and explicit exceptions.

    The master Event carries the rrule string; expansion happens at read time
    in recurrence.expand_recurrence(). Either `until` or `count` may be set;
    `rrule` may also encode its own terminator.

    Length caps:
      - rrule limited to 2048 chars (well above realistic RFC 5545 rules).
      - exceptions limited to 500 entries (caps memory + conflict-detection
        work for pathological inputs).
    """

    model_config = ConfigDict(frozen=True)

    rrule: str = Field(min_length=1, max_length=2048)
    exceptions: list[datetime] = Field(default_factory=list, max_length=500)
    until: datetime | None = None
    count: int | None = None


class Event(BaseModel):
    """A calendar event. workspace_id required — no default — multi-tenancy
    is enforced at construction.

    `fabric_object_id` is the optional pointer into ee/fabric so a Customer
    object can have a related Meeting event without a join table.
    `source_connector` + `source_external_id` track external-system origin
    (e.g. "gcalendar", "<google event id>") so sync.py can reconcile.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    workspace_id: str
    calendar_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str  # IANA timezone string, e.g. "America/Los_Angeles"
    created_by_user_id: str = Field(
        ...,
        description=(
            "User who created this event. Used by policy.check_event_modify "
            "to gate update/delete authz on synthetic-default calendars."
        ),
    )
    description: str = ""
    location: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    recurrence: Recurrence | None = None
    fabric_object_id: str | None = None
    source_connector: str | None = None
    source_external_id: str | None = None
    created_at: datetime
    updated_at: datetime


class Calendar(BaseModel):
    """A logical calendar belonging to a workspace.

    Visibility governs who can see events at all — read access is also
    gated by policy.check_calendar_read.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    workspace_id: str
    name: str
    owner_user_id: str
    timezone: str
    color: str = "#0A84FF"
    visibility: CalendarVisibility = CalendarVisibility.PUBLIC_TO_WORKSPACE
    shared_with_user_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class FreeBusy(BaseModel):
    """Availability fingerprint for a single attendee in a time window."""

    model_config = ConfigDict(frozen=True)

    attendee_email: str
    busy_periods: list[tuple[datetime, datetime]] = Field(default_factory=list)
