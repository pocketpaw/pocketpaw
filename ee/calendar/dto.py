# Calendar module — request/response DTOs.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H-NEW-1).
#
# Changes:
# - H-NEW-1: EventResponse now exposes created_by_user_id so the
#   frontend can render "created by X" badges and gate Edit/Delete
#   buttons on the client. CreateEventRequest deliberately omits it —
#   the server fills it from ctx.user_id; never trust the client to
#   self-attest the creator.
#
# Distinct request and response classes per operation. Requests are
# minimal (no workspace_id — that comes from the auth context). Responses
# mirror the domain value objects so clients see the full shape.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from ee.calendar.domain import (
    Attendee,
    ConflictSeverity,
    FreeBusy,
    Recurrence,
)

# ---------------------------------------------------------------------------
# Event request DTOs
# ---------------------------------------------------------------------------


class CreateEventRequest(BaseModel):
    """Request payload for POST /calendar/events.

    `attendees` is a list of Attendee value objects (email is the required
    field; everything else is optional).
    """

    calendar_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    starts_at: datetime
    ends_at: datetime
    timezone: str = Field(min_length=1)
    description: str = ""
    location: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    recurrence: Recurrence | None = None
    fabric_object_id: str | None = None

    @model_validator(mode="after")
    def _ends_after_starts(self) -> CreateEventRequest:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be strictly after starts_at")
        return self


class UpdateEventRequest(BaseModel):
    """Partial-update payload. At least one field must be set."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    location: str | None = None
    attendees: list[Attendee] | None = None
    recurrence: Recurrence | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateEventRequest:
        if all(
            v is None
            for v in (
                self.title,
                self.description,
                self.starts_at,
                self.ends_at,
                self.location,
                self.attendees,
                self.recurrence,
            )
        ):
            raise ValueError("at least one field must be provided")
        # If both starts_at and ends_at are present, validate order.
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise ValueError("ends_at must be strictly after starts_at")
        return self


class ListEventsRequest(BaseModel):
    """List/query parameters."""

    calendar_id: str | None = None
    starts_after: datetime
    starts_before: datetime
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def _window_valid(self) -> ListEventsRequest:
        if self.starts_before <= self.starts_after:
            raise ValueError("starts_before must be after starts_after")
        return self


# ---------------------------------------------------------------------------
# Event response DTOs
# ---------------------------------------------------------------------------


class EventResponse(BaseModel):
    """Full Event mirror returned by the API."""

    id: str
    workspace_id: str
    calendar_id: str
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    # H-NEW-1: surfaced to the client so the UI can render "created by X"
    # and decide whether to show Edit/Delete affordances. The server is
    # the source of truth; never accept this from the client on writes.
    created_by_user_id: str
    location: str | None
    attendees: list[Attendee]
    recurrence: Recurrence | None
    fabric_object_id: str | None
    source_connector: str | None
    source_external_id: str | None
    created_at: datetime
    updated_at: datetime


class EventListResponse(BaseModel):
    events: list[EventResponse]
    total: int


# ---------------------------------------------------------------------------
# FreeBusy DTOs
# ---------------------------------------------------------------------------


class FreeBusyRequest(BaseModel):
    """Compute availability for up to 50 attendees in a window."""

    attendee_emails: list[str] = Field(min_length=1, max_length=50)
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _window_valid(self) -> FreeBusyRequest:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be strictly after starts_at")
        return self


class FreeBusyResponse(BaseModel):
    freebusy: list[FreeBusy]


# ---------------------------------------------------------------------------
# Conflict DTOs
# ---------------------------------------------------------------------------


class ConflictReport(BaseModel):
    """Conflict detection result for a single event."""

    event_id: str
    conflicting_events: list[EventResponse] = Field(default_factory=list)
    severity: ConflictSeverity = ConflictSeverity.LOW
