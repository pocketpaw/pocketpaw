# Site templates — request / response schemas.
#
# Requests and responses are distinct models (cloud rule 4). The response is the
# template's metadata only: it has no snapshot, source or rippleSpec field, and
# ``extra="forbid"`` makes adding one by accident a construction error rather
# than a leak. ``owner`` is ``None`` for anyone but the owner, so a public
# template does not reveal who made it or where. ``preview_image_url`` is a
# public-rail https URL or ``None``, safe to show to any viewer.
#
# Updated 2026-10-01 (feat/discover-index): save and PATCH accept ``kind`` and
# ``audiences``; the response carries ``kind``, ``audiences`` and ``live_url``.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Visibility = Literal["private", "workspace", "public"]
Kind = Literal["site", "tool", "game"]
Audience = Literal["shop", "design", "everyone", "fun"]

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class SaveSiteTemplateRequest(BaseModel):
    """Body for ``POST /site-templates``: save the site pocket ``pocket_id`` as a
    template (private unless ``visibility`` says otherwise)."""

    model_config = ConfigDict(extra="forbid")

    pocket_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    visibility: Visibility = "private"
    kind: Kind = "site"
    audiences: list[Audience] = Field(default_factory=list, max_length=4)


class PatchSiteTemplateRequest(BaseModel):
    """Body for ``PATCH /site-templates/{id}``: any subset of the metadata."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    visibility: Visibility | None = None
    kind: Kind | None = None
    audiences: list[Audience] | None = Field(default=None, max_length=4)


class ListSiteTemplatesRequest(BaseModel):
    """Query for ``GET /site-templates``. ``scope``: ``mine`` (your own in this
    workspace), ``workspace`` (shared with this workspace) or ``public`` (shared
    with everyone, from every workspace)."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["mine", "workspace", "public"] = "mine"
    limit: int = Field(default=50, ge=1, le=50)
    cursor: str | None = None


class ReportSiteTemplateRequest(BaseModel):
    """Body for ``POST /site-templates/{id}/report``."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class UseSiteTemplateRequest(BaseModel):
    """Body for ``POST /site-templates/{id}/use``. ``name`` defaults to the
    template's name."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class SiteTemplateResponse(BaseModel):
    """Wire shape for one template: metadata only, never the snapshot."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    visibility: str
    version: int
    engine: str | None = None
    pattern: str | None = None
    owner: str | None = None
    is_mine: bool
    hidden: bool = False
    preview_image_url: str | None = None
    kind: str = "site"
    audiences: list[str] = Field(default_factory=list)
    live_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SiteTemplateListResponse(BaseModel):
    """A page of templates, newest first. Pass ``next_cursor`` back as ``cursor``
    for the next page; ``None`` marks the last one."""

    model_config = ConfigDict(extra="forbid")

    templates: list[SiteTemplateResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class UseSiteTemplateResponse(BaseModel):
    """Wire shape for ``use``: the id of the new site pocket."""

    model_config = ConfigDict(extra="forbid")

    pocket_id: str


__all__ = [
    "ListSiteTemplatesRequest",
    "PatchSiteTemplateRequest",
    "ReportSiteTemplateRequest",
    "SaveSiteTemplateRequest",
    "SiteTemplateListResponse",
    "SiteTemplateResponse",
    "UseSiteTemplateRequest",
    "UseSiteTemplateResponse",
]
