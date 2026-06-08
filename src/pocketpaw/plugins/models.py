# src/pocketpaw/plugins/models.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — Pydantic models for
# the Plugin Installer. Mirrors the SHAPE of ee/pocketpaw_ee/fleet/models.py
# (FleetInstallStep / FleetInstallReport) but copied into OSS so the core
# never imports pocketpaw_ee (import-linter forbids it).
"""Models for the .claude-plugin installer.

``PluginManifest`` is the structurally-validated view of a repo's
``.claude-plugin/plugin.json``. ``PluginInstallStep`` /
``PluginInstallReport`` describe an install run so the UI can show partial
progress (succeeded / skipped / failed) without re-running everything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PluginManifest(BaseModel):
    """Parsed ``.claude-plugin/plugin.json``.

    Only ``name`` is required; the rest mirror the optional fields the
    Claude Code plugin standard allows. ``skills`` is an optional path
    override for where the plugin's ``SKILL.md`` directories live (defaults
    to the conventional ``skills/`` directory when unset).
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    skills: str | None = None


class PluginInstallStep(BaseModel):
    """One step in the install pipeline.

    Reports ``succeeded`` / ``skipped`` / ``failed`` so the UI can show
    partial progress. Steps never raise out of the installer — failures are
    captured here.
    """

    name: str
    status: Literal["succeeded", "skipped", "failed"]
    detail: str = ""


class PluginInstallReport(BaseModel):
    """Full report of a plugin install run."""

    plugin: str
    installed_at: datetime = Field(default_factory=datetime.now)
    steps: list[PluginInstallStep] = Field(default_factory=list)
    installed_skills: list[str] = Field(default_factory=list)

    def succeeded(self) -> bool:
        return all(step.status != "failed" for step in self.steps)

    def failed_steps(self) -> list[PluginInstallStep]:
        return [s for s in self.steps if s.status == "failed"]
