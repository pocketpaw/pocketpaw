# dto.py — request/response Pydantic schemas for the Projects entity.
# Created: 2026-05-16 — Mission Control backend completion. Distinct
#   *Request and *Response models per ee/cloud Code Rule §4 — never reuse
#   one model for input and output. Domain → wire mapper lives at the
#   bottom alongside the response shapes.
"""Projects entity — request/response DTOs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.projects.domain import Project

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateProjectRequest(BaseModel):
    """Body for ``POST /projects``.

    ``name`` is the only required field. ``status`` defaults to ``active``
    — archived projects are created via the dedicated archive endpoint
    rather than through this create path, so the typical flow lands a
    fresh, pickable project on the workspace.
    """

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    color: str = Field(default="", max_length=16)  # "#RRGGBB" or empty
    lead_id: str | None = None
    status: Literal["active", "archived"] = "active"


class UpdateProjectRequest(BaseModel):
    """Body for ``PATCH /projects/{id}``.

    Every field is optional; only keys the caller explicitly provides are
    touched. ``status`` transitions through this endpoint are allowed
    (active → archived and back), but the dedicated archive endpoint is
    the canonical path for the "Archive" action in the UI.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    color: str | None = Field(default=None, max_length=16)
    lead_id: str | None = None
    status: Literal["active", "archived"] | None = None


class ListProjectsRequest(BaseModel):
    """Filters for ``GET /projects``.

    All optional. ``status`` filters down to active vs archived (default:
    only active). ``limit`` caps the projected list so the picker stays
    responsive even on workspaces with hundreds of historical projects.
    """

    status: Literal["active", "archived"] | None = None
    limit: int = Field(default=100, ge=1, le=500)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class ProjectResponse(BaseModel):
    """Wire shape returned by every Project endpoint.

    Timestamps are ISO-8601 strings (always tz-aware, ``+00:00`` suffix)
    so the desktop client's ``new Date(...)`` parses unambiguously.
    """

    id: str
    workspace_id: str
    name: str
    description: str
    color: str
    lead_id: str | None = None
    status: str
    created_by: str
    created_at: str | None = None
    updated_at: str | None = None


def project_to_dto(project: Project) -> ProjectResponse:
    """Map a domain :class:`Project` to its wire DTO.

    Mechanical mapping because field names align — domain stays
    snake_case, wire stays snake_case. The PocketPaw frontend's
    camelCase adapter handles the rendering side.
    """

    return ProjectResponse(
        id=str(project.id),
        workspace_id=project.workspace_id,
        name=project.name,
        description=project.description,
        color=project.color,
        lead_id=project.lead_id,
        status=project.status,
        created_by=project.created_by,
        created_at=iso_utc(project.created_at),
        updated_at=iso_utc(project.updated_at),
    )


__all__ = [
    "CreateProjectRequest",
    "ListProjectsRequest",
    "ProjectResponse",
    "UpdateProjectRequest",
    "project_to_dto",
]
