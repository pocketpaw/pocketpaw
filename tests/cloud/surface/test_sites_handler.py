# tests/cloud/surface/test_sites_handler.py — Sites surface handler.
#
# Created: 2026-06-03 — Guards the /sites surface preamble. The create-site
# skill can't load under the chat agent's `setting_sources=[]` SDK config
# (skill discovery disabled for persona isolation), so the create→publish
# PROCEDURE has to live in the always-on /sites preamble instead. These
# tests assert the preamble carries:
#   1. The orientation it already had — surface kind="sites", talk "site"
#      not "pocket" as the deliverable.
#   2. The folded-in procedure — the create MCP tool, the publish MCP tool,
#      and a lead-capture form with named fields so the published site
#      captures leads out of the box.

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


async def test_sites_handler_carries_create_publish_procedure() -> None:
    """The preamble folds the create→publish two-step in via the MCP tools."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    # Step 1 — create the source pocket via the pocket specialist MCP tool.
    assert "mcp__pocketpaw_pocket_specialist__create" in preamble
    # Step 2 — publish it as a live site via the sites manager MCP tool.
    assert "mcp__pocketpaw_sites_manager__publish" in preamble


async def test_sites_handler_specifies_lead_capture_form() -> None:
    """The procedure asks for a lead-capture form with clear field names."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    lower = preamble.lower()
    # A form is part of the marketing/landing build.
    assert "form" in lower
    # At least one concrete, named field so leads are actually captured.
    assert "email" in lower or "full_name" in lower
