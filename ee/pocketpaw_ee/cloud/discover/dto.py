# Discover — request / response schemas.
#
# Created 2026-10-01 (feat/discover-index). ``PublicListingResponse`` is an
# allow-list served to anonymous viewers: exactly ``id, source, kind, title,
# description, audiences, featured, preview_image_url, live_url, remix_count,
# created_at``. Never ``workspace``, ``owner``, ``reports``, ``hidden`` or
# ``source_id``; ``extra="forbid"`` makes adding one by accident a construction
# error rather than a leak.
# Updated 2026-10-01 (feat/discover-index): ``UseListingRequest`` (optional
# ``name``) for ``POST /discover/{id}/use``.
# Updated 2026-10-02 (feat/discover-moderation): staff-only
# ``ListStaffListingsRequest`` / ``StaffListingResponse`` / ``StaffListingPage``
# for the platform moderation routes. Never served on a public route.

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class ListPublicListingsRequest(BaseModel):
    """Query for the public index. ``audience`` matches one of a listing's
    audiences; ``q`` is a case-insensitive substring of title or description.
    Newest first; pass ``next_cursor`` back as ``cursor``."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=32)
    audience: str | None = Field(default=None, max_length=32)
    q: str | None = Field(default=None, max_length=100)
    featured: bool | None = None
    cursor: str | None = None
    limit: int = Field(default=24, ge=1, le=50)


class ListStaffListingsRequest(BaseModel):
    """Query for the staff moderation list: every listing, hidden ones included.
    ``hidden`` / ``featured`` filter on the flag; ``q`` as on the public list."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=64)
    hidden: bool | None = None
    featured: bool | None = None
    q: str | None = Field(default=None, max_length=100)
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ReportListingRequest(BaseModel):
    """Body for reporting a listing."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class UseListingRequest(BaseModel):
    """Body for ``POST /discover/{id}/use``. ``name`` defaults to the source
    item's name."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)


class UpsertListingRequest(BaseModel):
    """The source-owned fields a source sync writes onto a listing."""

    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    audiences: list[str] = Field(default_factory=list, max_length=8)
    preview_image_url: str | None = None
    live_url: str | None = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class PublicListingResponse(BaseModel):
    """One public Discover card (an allow-list; see the module header)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    kind: str
    title: str
    description: str
    audiences: list[str]
    featured: bool
    preview_image_url: str | None
    live_url: str | None
    remix_count: int
    created_at: datetime | None


class PublicListingPage(BaseModel):
    """A page of public listings, newest first; ``next_cursor`` is ``None`` on
    the last page."""

    model_config = ConfigDict(extra="forbid")

    items: list[PublicListingResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class StaffListingResponse(BaseModel):
    """One listing as staff see it, with its moderation state. STAFF ONLY: it
    carries ``workspace_id``, ``owner`` and ``source_id``, so it must never be
    reused for a public read (that is ``PublicListingResponse``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    source_id: str
    workspace_id: str
    owner: str
    kind: str
    title: str
    description: str
    live_url: str | None
    featured: bool
    hidden: bool
    report_count: int
    dismissed_reporter_count: int
    remix_count: int
    created_at: datetime | None


class StaffListingPage(BaseModel):
    """A page of staff listings, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[StaffListingResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class UseListingResponse(BaseModel):
    """What ``use`` made: the source's own result (``{pocket_id}`` for a site
    template)."""

    model_config = ConfigDict(extra="forbid")

    source: str
    result: dict[str, Any]


__all__ = [
    "ListPublicListingsRequest",
    "ListStaffListingsRequest",
    "PublicListingPage",
    "PublicListingResponse",
    "ReportListingRequest",
    "StaffListingPage",
    "StaffListingResponse",
    "UpsertListingRequest",
    "UseListingRequest",
    "UseListingResponse",
]
