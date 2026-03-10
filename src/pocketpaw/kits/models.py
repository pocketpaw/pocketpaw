"""PawKit Pydantic models.

Defines the configuration schema for PawKits — installable dashboard
layouts that combine metrics, tables, kanban boards, feeds, and more.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class PawKitMeta(BaseModel):
    """Kit metadata for display and marketplace indexing."""

    name: str
    author: str
    version: str
    description: str
    category: str
    tags: list[str] = []
    icon: str = "layout-dashboard"
    built_in: bool = False


class MetricItem(BaseModel):
    """Single metric card within a metrics-row panel."""

    label: str
    source: str  # "workflow:<id>" or "api:<endpoint>"
    field: str
    format: Literal["number", "currency", "percent", "text"] = "number"
    trend: bool = False


class PanelConfig(BaseModel):
    """Individual panel widget configuration.

    The ``type`` field selects which renderer to use. Type-specific
    properties are captured via ``model_config(extra="allow")``.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["metrics-row", "table", "kanban", "chart", "feed", "markdown"]


class SectionConfig(BaseModel):
    """A titled section containing one or more panels."""

    title: str
    span: Literal["full", "left", "right"] = "full"
    panels: list[PanelConfig]


class LayoutConfig(BaseModel):
    """Grid layout definition for the command center."""

    columns: int = 2
    sections: list[SectionConfig]


class WorkflowConfig(BaseModel):
    """Scheduled or trigger-based automated workflow."""

    schedule: str | None = None
    trigger: dict[str, Any] | None = None
    instruction: str
    output_type: Literal["structured", "feed", "task_list", "document"] = "structured"
    retry: int | None = None


class UserConfigField(BaseModel):
    """User-configurable field shown during kit install."""

    key: str
    label: str
    type: Literal["text", "secret", "select", "number"] = "text"
    placeholder: str | None = None
    options: list[str] | None = None
    help_url: str | None = None


class PawKitConfig(BaseModel):
    """Root configuration model for a PawKit."""

    meta: PawKitMeta
    layout: LayoutConfig
    workflows: dict[str, WorkflowConfig] = {}
    user_config: list[UserConfigField] | None = None
    skills: list[str] | None = None
    integrations: dict[str, Any] | None = None


class InstalledKit(BaseModel):
    """An installed kit with user-specific values and activation state."""

    id: str
    config: PawKitConfig
    user_values: dict[str, str] = {}
    installed_at: str
    active: bool = False
