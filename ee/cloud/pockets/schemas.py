"""Pockets domain — request/response schemas.

Changes: Added agents, rippleSpec (aliased), and widgets fields to CreatePocketRequest
so the frontend can pass the full pocket spec on creation instead of requiring
separate follow-up calls.
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
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    session_id: str | None = Field(default=None, alias="sessionId")
    agents: list[str] = Field(default_factory=list)  # Agent IDs to assign
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")
    widgets: list[dict] = Field(default_factory=list)  # Initial widget definitions

    model_config = {"populate_by_name": True}


class UpdatePocketRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None
    icon: str | None = None
    color: str | None = None
    visibility: str | None = None
    ripple_spec: dict | None = Field(default=None, alias="rippleSpec")

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
    created_at: datetime
    updated_at: datetime
