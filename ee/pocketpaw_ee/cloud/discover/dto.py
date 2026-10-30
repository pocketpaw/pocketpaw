# Discover — request / response schemas.
#
# ``PublicListingResponse`` is an allow-list served to anonymous viewers: exactly
# ``id, slug, source, kind, title, description, audiences, tags, featured,
# preview_image_url, live_url, remix_count, created_at, updated_at, media_kind,
# media_url, creator, remixed_from``. Never ``workspace``, ``owner``, ``reports``,
# ``hidden``, ``source_id`` or any user id or email; ``extra="forbid"`` (here and
# on ``PublicCreator`` / ``RemixedFrom``) makes adding one by accident a
# construction error rather than a leak. ``slug`` is the stable URL handle (see
# the listing model); the public item route takes it in place of the id.
# ``creator`` is the owner's display name only, and only when the source set
# ``show_creator``; ``remixed_from`` names the listing this item was made from.
#
# ``UpsertListingRequest`` is what a source sync writes; ``extra="forbid"`` so a
# source can't smuggle a Discover-owned field. Tags are normalised on write by
# ``_core.tags.normalize_tags``. The staff-only ``ListStaffListingsRequest`` /
# ``StaffListingResponse`` / ``StaffListingPage`` serve the platform moderation
# routes and are never served on a public route.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pocketpaw_ee.cloud._core.tags import MAX_TAG_LENGTH, normalize_tag, normalize_tags

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class ListPublicListingsRequest(BaseModel):
    """Query for the public index. ``audience`` matches one of a listing's
    audiences, ``tag`` one of its tags (normalised like a stored tag); ``q`` is a
    case-insensitive substring of title or description. Newest first; pass
    ``next_cursor`` back as ``cursor``."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=32)
    audience: str | None = Field(default=None, max_length=32)
    tag: str | None = Field(default=None, max_length=MAX_TAG_LENGTH + 8)
    q: str | None = Field(default=None, max_length=100)
    featured: bool | None = None
    cursor: str | None = None
    limit: int = Field(default=24, ge=1, le=50)

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str | None) -> str | None:
        # Never raises (this model is a FastAPI query dependency, where a raise
        # would be a 500): a tag outside the dialect simply matches nothing.
        if value is None:
            return None
        try:
            return normalize_tag(value) or None
        except ValueError:
            return value.strip().lower()


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
    media_kind: Literal["image", "video", "audio"] | None = None
    media_url: str | None = None
    #: At most 8, each 1-32 of ``[a-z0-9-]``; normalised (see ``_core.tags``).
    tags: list[str] = Field(default_factory=list)
    #: The owner opted in to their display name on the public card.
    show_creator: bool = False
    #: The id, in this same source, of the item this one was made from.
    remixed_from_source_id: str | None = Field(default=None, min_length=1, max_length=64)
    #: Optional handle the source proposes; slugified like ``title`` and only
    #: used when the listing has no slug yet (slugs never change afterwards).
    slug: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str]:
        return normalize_tags(value)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class PublicCreator(BaseModel):
    """Who made a listing, as the public sees it: a display name, nothing else."""

    model_config = ConfigDict(extra="forbid")

    name: str


class RemixedFrom(BaseModel):
    """The public listing this one was made from."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str


class PublicListingResponse(BaseModel):
    """One public Discover card (an allow-list; see the module header).
    ``updated_at`` is when the listing's public content (or its featured /
    hidden state) last changed; a re-sync that changes nothing leaves it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    source: str
    kind: str
    title: str
    description: str
    audiences: list[str]
    tags: list[str] = Field(default_factory=list)
    featured: bool
    preview_image_url: str | None
    live_url: str | None
    remix_count: int
    created_at: datetime | None
    updated_at: datetime | None = None
    media_kind: str | None = None
    media_url: str | None = None
    creator: PublicCreator | None = None
    remixed_from: RemixedFrom | None = None


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
    slug: str
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
    "PublicCreator",
    "PublicListingPage",
    "PublicListingResponse",
    "RemixedFrom",
    "ReportListingRequest",
    "StaffListingPage",
    "StaffListingResponse",
    "UpsertListingRequest",
    "UseListingRequest",
    "UseListingResponse",
]
