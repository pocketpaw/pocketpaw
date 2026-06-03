# tests/cloud/surface/test_sites_handler.py — Sites surface handler.
#
# Created: 2026-06-03 — Guards the /sites surface preamble.
# Updated: 2026-06-03 (pm) — Bundled skills now load on the SDK backend (local
# plugin via the SDK `plugins=` option), so the preamble PREFERS the
# `pocketpaw-create-site` skill and keeps the raw MCP tools only as a fallback.
# These tests assert the preamble carries:
#   1. The orientation — surface kind="sites", talk "site" not "pocket".
#   2. The preferred path — the `pocketpaw-create-site` skill.
#   3. The fallback path — the create MCP tool + the publish MCP tool — still
#      present so the flow never breaks when the skill is unavailable.
#   4. A lead-capture form with named fields so the published site captures
#      leads out of the box.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import sites as sites_handler

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-surface-sites"
USER = "u-sites"


async def test_sites_handler_carries_orientation() -> None:
    """The preamble still orients: surface=sites, build a site (not a pocket)."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    assert '<surface kind="sites"' in preamble
    # Talks about the deliverable as a site/page.
    assert "site" in preamble.lower()
    # Must NOT frame the deliverable as a pocket — the agent kept building
    # in-app pockets instead of publishable sites (the reported drift).
    assert "build a pocket" not in preamble.lower()
    assert "build a 'pocket'" not in preamble.lower()


async def test_sites_handler_prefers_create_site_skill() -> None:
    """The preamble points the agent at the create-site skill as the primary
    path now that bundled skills load on the SDK backend."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    assert "pocketpaw-create-site" in preamble
    # It must be framed as the preferred route, not an afterthought.
    assert "prefer" in preamble.lower()


async def test_sites_handler_keeps_mcp_fallback() -> None:
    """The raw MCP tools remain as a fallback so the create→publish flow never
    breaks when the skill is unavailable (e.g. sdk_load_bundled_skills off, or a
    backend without the bundled plugin)."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    # Step 1 — create the source pocket via the pocket specialist MCP tool.
    assert "mcp__pocketpaw_pocket_specialist__create" in preamble
    # Step 2 — publish it as a live site via the sites manager MCP tool.
    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    # Framed as a fallback, not the primary instruction.
    assert "fall back" in preamble.lower() or "fallback" in preamble.lower()


async def test_sites_handler_specifies_lead_capture_form() -> None:
    """The procedure asks for a lead-capture form with clear field names."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    lower = preamble.lower()
    # A form is part of the marketing/landing build.
    assert "form" in lower
    # At least one concrete, named field so leads are actually captured.
    assert "email" in lower or "full_name" in lower
