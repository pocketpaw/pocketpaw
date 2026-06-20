# src/pocketpaw/plugins/models.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — Pydantic models for
# the Plugin Installer. Mirrors the SHAPE of ee/pocketpaw_ee/fleet/models.py
# (FleetInstallStep / FleetInstallReport) but copied into OSS so the core
# never imports pocketpaw_ee (import-linter forbids it).
# Updated: 2026-06-08 (feat/plugin-installer-mcp) — added
# ``PluginManifest.mcp_servers`` (optional path override for the bundle's
# MCP config) and ``PluginInstallReport.installed_mcp_servers`` (the
# namespaced MCP server names registered on install).
# Updated: 2026-06-08 (feat/plugin-installer-listremove) — added the
# list/remove models: ``InstalledPlugin`` (a registry entry view returned
# by ``list_plugins``) and ``PluginRemoveReport`` (a step-by-step remove
# run, mirroring ``PluginInstallReport``'s per-component degradation).
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
    to the conventional ``skills/`` directory when unset). ``mcp_servers``
    is an optional path override for the bundle's MCP config file (defaults
    to ``.mcp.json`` at the plugin root when unset).
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    skills: str | None = None
    # Optional path override for the bundle's MCP config file. Defaults to
    # the conventional ``.mcp.json`` at the plugin root when unset.
    mcp_servers: str | None = None


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
    installed_mcp_servers: list[str] = Field(default_factory=list)

    def succeeded(self) -> bool:
        return all(step.status != "failed" for step in self.steps)

    def failed_steps(self) -> list[PluginInstallStep]:
        return [s for s in self.steps if s.status == "failed"]


class InstalledPlugin(BaseModel):
    """A single installed-plugin view, sourced from the registry entry.

    Returned by ``list_plugins``. ``installed_at`` is kept as a plain string
    (the registry stores an ISO timestamp produced by ``datetime.isoformat``)
    so a malformed/legacy entry can never make the whole listing fail to
    serialise.
    """

    name: str
    version: str = "0.0.0"
    source: str = ""
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    installed_at: str = ""


class PluginRemoveReport(BaseModel):
    """Full report of a plugin removal run.

    Mirrors :class:`PluginInstallReport`: each component (skill dir, MCP
    server) is one :class:`PluginInstallStep`, and a per-component failure
    degrades to a ``failed`` / ``skipped`` step rather than raising mid-remove
    (only an unknown plugin raises up front). The registry entry is dropped
    even when some components could not be cleaned up, so a half-removed
    plugin never lingers in the listing.
    """

    plugin: str
    removed_at: datetime = Field(default_factory=datetime.now)
    steps: list[PluginInstallStep] = Field(default_factory=list)
    removed_skills: list[str] = Field(default_factory=list)
    removed_mcp_servers: list[str] = Field(default_factory=list)

    def succeeded(self) -> bool:
        return all(step.status != "failed" for step in self.steps)

    def failed_steps(self) -> list[PluginInstallStep]:
        return [s for s in self.steps if s.status == "failed"]
