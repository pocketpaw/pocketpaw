# Site templates — request / response schemas.
#
# Requests and responses are distinct models (cloud rule 4). The response is the
# template's metadata only: it has no snapshot, source or rippleSpec field, and
# ``extra="forbid"`` makes adding one by accident a construction error rather
# than a leak.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class SaveSiteTemplateRequest(BaseModel):
    """Body for ``POST /site-templates``: save the site pocket ``pocket_id`` as a
    private template."""

    model_config = ConfigDict(extra="forbid")

    pocket_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


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
    owner: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UseSiteTemplateResponse(BaseModel):
    """Wire shape for ``use``: the id of the new site pocket."""

    model_config = ConfigDict(extra="forbid")

    pocket_id: str


__all__ = [
    "SaveSiteTemplateRequest",
    "SiteTemplateResponse",
    "UseSiteTemplateRequest",
    "UseSiteTemplateResponse",
]
