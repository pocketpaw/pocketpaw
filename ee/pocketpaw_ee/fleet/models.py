# ee/fleet/models.py — FleetTemplate manifest + install report types.
# Created: 2026-04-13 (Move 7 PR-B) — A fleet is a thin orchestration over
# primitives that already exist (soul template, pocket, connectors, scope).
# No new runtime concepts; the manifest just names them in one place so a
# non-technical operator can install the whole bundle in one step.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class FleetConnector(BaseModel):
    """One connector to register when the fleet is installed."""

    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    optional: bool = False  # Skip silently if the connector module is missing.


class FleetTemplate(BaseModel):
    """An installable bundle of soul + pocket + connectors + scopes."""

    name: str
    display_name: str = ""
    description: str = ""
    version: str = "0.1.0"
    soul_template: str  # Bundled soul template name (arrow / flash / cyborg / analyst)
    soul_name: str = ""  # Override; defaults to template's name
    pocket_name: str  # Pocket created at install time
    pocket_description: str = ""
    pocket_widgets: list[dict[str, Any]] = Field(default_factory=list)
    connectors: list[FleetConnector] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FleetInstallStep(BaseModel):
    """One step in the install pipeline. Reports succeeded / skipped / failed
    so the UI can show partial progress without re-running the whole install.
    """

    name: str
    status: Literal["succeeded", "skipped", "failed"]
    detail: str = ""
    duration_ms: int = 0


class FleetInstallReport(BaseModel):
    """Full report of an install run."""

    fleet: str
    installed_at: datetime = Field(default_factory=datetime.now)
    steps: list[FleetInstallStep] = Field(default_factory=list)
    soul_id: str | None = None
    pocket_id: str | None = None

    def succeeded(self) -> bool:
        return all(step.status != "failed" for step in self.steps)

    def failed_steps(self) -> list[FleetInstallStep]:
        return [s for s in self.steps if s.status == "failed"]
