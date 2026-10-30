# src/pocketpaw/plugins/__init__.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — Plugin Installer
# package. Adopts the .claude-plugin standard: clone a GitHub repo, read
# .claude-plugin/plugin.json, install the repo's skills into the skill
# loader path, and record the install. Skills-only slice (MCP + list/remove
# ship separately).

from __future__ import annotations

from pocketpaw.plugins.installer import PluginInstaller
from pocketpaw.plugins.models import (
    PluginInstallReport,
    PluginInstallStep,
    PluginManifest,
)

__all__ = [
    "PluginInstallReport",
    "PluginInstallStep",
    "PluginInstaller",
    "PluginManifest",
]
