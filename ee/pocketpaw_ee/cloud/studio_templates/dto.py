# Studio templates — request / response schemas.
#
# Created 2026-10-02 (feat/studio-templates). Requests and responses are distinct
# models. ``cover.url`` / ``cover.poster_url`` stay backend-relative
# (``/api/v1/media/...``) here; only the Discover listing absolutizes them.
# ``recipe.params`` never carries input-image fields (stripped on publish).

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Visibility = Literal["private", "workspace", "public"]
Audience = Literal["shop", "design", "everyone", "fun"]

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class PublishStudioTemplateRequest(BaseModel):
    """Body for ``POST /studio-templates``: publish one asset of the generation
    ``generation_id`` (its first asset when ``asset_id`` is omitted)."""

    model_config = ConfigDict(extra="forbid")

    generation_id: str = Field(min_length=1)
    asset_id: str | None = Field(default=None, min_length=1)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    audiences: list[Audience] = Field(default_factory=list, max_length=4)
    visibility: Visibility = "private"


class PatchStudioTemplateRequest(BaseModel):
    """Body for ``PATCH /studio-templates/{id}``: any subset of the metadata."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    audiences: list[Audience] | None = Field(default=None, max_length=4)
    visibility: Visibility | None = None


class ListStudioTemplatesRequest(BaseModel):
    """Query for ``GET /studio-templates``: the caller's own, newest first."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=50)
    cursor: str | None = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class StudioTemplateCover(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    mime: str
    width: int | None = None
    height: int | None = None
    poster_url: str | None = None


class StudioTemplateRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    model: str
    prompt: str
    params: dict[str, Any] = Field(default_factory=dict)


class StudioTemplateResponse(BaseModel):
    """Wire shape for one template, as its owner sees it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    owner: str
    template_type: str
    source_generation_id: str
    kind: str
    title: str
    description: str
    audiences: list[str]
    visibility: str
    cover: StudioTemplateCover
    recipe: StudioTemplateRecipe
    uses_input_images: bool
    hidden: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StudioTemplateListResponse(BaseModel):
    """A page of the caller's templates, newest first; ``next_cursor`` is
    ``None`` on the last page."""

    model_config = ConfigDict(extra="forbid")

    templates: list[StudioTemplateResponse] = Field(default_factory=list)
    next_cursor: str | None = None


__all__ = [
    "ListStudioTemplatesRequest",
    "PatchStudioTemplateRequest",
    "PublishStudioTemplateRequest",
    "StudioTemplateCover",
    "StudioTemplateListResponse",
    "StudioTemplateRecipe",
    "StudioTemplateResponse",
]
