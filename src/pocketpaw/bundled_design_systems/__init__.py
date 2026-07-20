# bundled_design_systems/__init__.py — the bundled library of portable
# DESIGN.md design systems that ship with PocketPaw as package data.
#
# Created: 2026-07-06 (feat/sites-crew-design-systems, SC-7b). Sibling in spirit
# to ``pocketpaw.bundled_kb`` and ``pocketpaw.bundled_skills``: where bundled_kb
# ships pre-compiled kb-go scopes and bundled_skills ships on-demand SKILL.md
# workflows, this package ships a curated set of ORIGINAL, brand-grade design
# systems in the DESIGN.md format from our 2026-07-06 sites-design-system
# research (docs/design/drafts/2026-07-06-sites-design-system-research.md).
#
# Each ``_bundled/<slug>/`` directory holds three files that are the SAME tokens
# in three shapes:
#   * ``DESIGN.md``    — YAML front-matter (machine-readable tokens: full 50–900
#                        color scales, a display→caption type scale, spacing,
#                        rounded, elevation, and component tokens with states) +
#                        a markdown body (Overview · Colors · Typography · Layout
#                        · Elevation & Depth · Shapes · Components · Do's and
#                        Don'ts) carrying the rationale + anti-patterns.
#   * ``tokens.css``   — the same tokens compiled to CSS custom properties; the
#                        shared source of truth both render engines consume
#                        (ripple maps tokens → theme, svelte imports the CSS).
#   * ``manifest.json``— the index metadata (slug/name/description + the
#                        aesthetic × industries × page_types taxonomy) the
#                        retriever lists so the authoring agent can CHOOSE a
#                        coherent identity instead of inventing one cold.
#
# This module is pure package data + a path helper. The retriever that reads
# these files lives in the enterprise layer
# (``pocketpaw_ee.agent.mcp_servers.design_systems``) and reaches the bundle
# through ``bundled_design_systems_dir()`` — no kb-go, no network. EE depends on
# OSS core, so that import is allowed.
#
# Packaging: the files ship in the wheel automatically because the root
# ``pyproject.toml`` wheel target uses ``only-include = ["src/pocketpaw"]``,
# which captures every file under this package recursively (the same mechanism
# that ships ``bundled_kb``'s ``index.json`` and ``bundled_skills``'s ``.md``).
"""Bundled library of portable DESIGN.md design systems (package data)."""

from __future__ import annotations

from pathlib import Path

__all__ = ["bundled_design_systems_dir"]


def bundled_design_systems_dir() -> Path:
    """Return the on-disk directory holding the bundled ``<slug>/`` systems.

    Each child directory is one design system carrying ``DESIGN.md`` +
    ``tokens.css`` + ``manifest.json``. Callers (the EE retriever) iterate this
    directory to list systems and read a single system's files by slug.
    """
    return Path(__file__).parent / "_bundled"
