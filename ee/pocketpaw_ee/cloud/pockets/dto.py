"""Pockets domain — request/response schemas.

Changes: Added agents, rippleSpec (aliased), and widgets fields to CreatePocketRequest
so the frontend can pass the full pocket spec on creation instead of requiring
separate follow-up calls.

Updated: 2026-05-16 — added optional ``project_id`` (aliased as
``projectId`` on the wire) to CreatePocketRequest / UpdatePocketRequest /
PocketResponse so pockets can be grouped under a Mission Control Project.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreatePocketRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    type: str = "custom"
    icon: str = ""
    color: str = ""
    visibility: str = Field(default="workspace", pattern="^(private|workspace|public)$")
    session_id: str | None = Field(default=None, alias="sessionId")
    agents: list[str] = Field(default_factory=list)  # Agent IDs to assign
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")
    widgets: list[dict] = Field(default_factory=list)  # Initial widget definitions
    project_id: str | None = Field(default=None, alias="projectId")

    model_config = {"populate_by_name": True}


class UpdatePocketRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None
    icon: str | None = None
    color: str | None = None
    visibility: str | None = None
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")
    project_id: str | None = Field(default=None, alias="projectId")

    model_config = {"populate_by_name": True}


class AddWidgetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = "custom"
    icon: str = ""
    color: str = ""
    span: str = "col-span-1"
    data_source_type: str = "static"
    config: dict = Field(default_factory=dict)
    props: dict = Field(default_factory=dict)
    assigned_agent: str | None = None


class UpdateWidgetRequest(BaseModel):
    name: str | None = None
    type: str | None = None
    icon: str | None = None
    config: dict | None = None
    props: dict | None = None
    data: Any = None
    assigned_agent: str | None = None


class ReorderWidgetsRequest(BaseModel):
    widget_ids: list[str]  # Ordered list of widget IDs


class ShareLinkRequest(BaseModel):
    access: str = Field(default="view", pattern="^(view|comment|edit)$")


class AddCollaboratorRequest(BaseModel):
    user_id: str
    access: str = Field(default="edit", pattern="^(view|comment|edit)$")


class PocketResponse(BaseModel):
    id: str
    workspace: str
    name: str
    description: str
    type: str
    icon: str
    color: str
    owner: str
    visibility: str
    team: list[Any]
    agents: list[Any]
    widgets: list[dict]
    ripple_spec: dict | None = None
    share_link_token: str | None = None
    share_link_access: str = "view"
    shared_with: list[str]
    project_id: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Domain → wire mappers (Phase 8)
# ---------------------------------------------------------------------------


def pocket_to_wire_dict(p) -> dict:
    """Convert a domain ``Pocket`` (from ``ee.cloud.pockets.domain``) to
    the legacy wire-format dict. Byte-equivalent to the
    ``_pocket_response`` helper in ``service.py``.

    Also applies read-time normalization to ``rippleSpec``: old pockets
    persisted before the agent-alias safety net (``root`` / ``tree`` /
    etc. lifted into ``ui``) get fixed in flight without a DB rewrite.
    The normalizer is idempotent — specs already in the canonical
    ``{ui, state}`` shape pass through unchanged.
    """
    from pocketpaw_ee.cloud._core.time import iso_utc
    from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

    return {
        "_id": p.id,
        "workspace": p.workspace_id,
        "name": p.name,
        "description": p.description,
        "type": p.type,
        "icon": p.icon,
        "color": p.color,
        "owner": p.owner,
        "visibility": p.visibility,
        "team": list(p.team),
        "agents": list(p.agents),
        "widgets": [_widget_to_wire(w) for w in p.widgets],
        "rippleSpec": normalize_ripple_spec(p.ripple_spec) if p.ripple_spec else p.ripple_spec,
        "shareLinkToken": p.share_link_token,
        "shareLinkAccess": p.share_link_access,
        "sharedWith": list(p.shared_with),
        "projectId": p.project_id,
        "createdAt": iso_utc(p.created_at),
        "updatedAt": iso_utc(p.updated_at),
    }


def _widget_to_wire(w) -> dict:
    """Convert a domain ``Widget`` to the legacy wire-format dict. The
    Beanie model's ``model_dump(by_alias=True)`` produces the same shape
    so this just rebuilds it from the frozen dataclass."""
    return {
        "_id": w.id,
        "name": w.name,
        "type": w.type,
        "icon": w.icon,
        "color": w.color,
        "span": w.span,
        "dataSourceType": w.data_source_type,
        "config": dict(w.config),
        "props": dict(w.props),
        "data": w.data,
        "assignedAgent": w.assigned_agent,
        "position": {"row": w.position.row, "col": w.position.col},
    }
