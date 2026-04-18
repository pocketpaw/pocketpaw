"""Pocket and Widget documents."""

from __future__ import annotations

from typing import Any

from beanie import Indexed
from bson import ObjectId
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class WidgetPosition(BaseModel):
    row: int = 0
    col: int = 0


class Widget(BaseModel):
    """Widget subdocument embedded in a Pocket.

    Has its own _id so the frontend can address widgets by ID (not index).
    Field aliases match the frontend camelCase convention.
    """

    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    name: str
    type: str = "custom"
    icon: str = ""
    color: str = ""
    span: str = "col-span-1"
    dataSourceType: str = Field(default="static", alias="dataSourceType")
    config: dict[str, Any] = Field(default_factory=dict)
    props: dict[str, Any] = Field(default_factory=dict)
    data: Any = None
    assignedAgent: str | None = Field(default=None, alias="assignedAgent")
    position: WidgetPosition = Field(default_factory=WidgetPosition)

    model_config = {"populate_by_name": True}


class Pocket(TimestampedDocument):
    """Pocket workspace with widgets, team, and ripple spec."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    description: str = ""
    type: str = "custom"  # no pattern restriction — frontend sends data, deep-work, etc.
    icon: str = ""
    color: str = ""
    owner: str
    team: list[Any] = Field(default_factory=list)  # User IDs or populated objects
    agents: list[Any] = Field(default_factory=list)  # Agent IDs or populated objects
    widgets: list[Widget] = Field(default_factory=list)
    rippleSpec: dict[str, Any] | None = Field(default=None, alias="rippleSpec")
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    share_link_token: str | None = None
    share_link_access: str = Field(default="view", pattern="^(view|comment|edit)$")
    shared_with: list[str] = Field(default_factory=list)  # User IDs with explicit access

    model_config = {"populate_by_name": True}

    class Settings:
        name = "pockets"
