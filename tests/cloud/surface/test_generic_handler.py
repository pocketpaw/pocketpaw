# tests/cloud/surface/test_generic_handler.py — Generic fallback handler.
#
# Created: 2026-05-24 — One guarantee: the generic handler always
# returns a valid preamble naming surface=generic, even when meta is
# completely empty. It's the catch-all every other failure mode reaches
# for, so it must never raise and must always carry the surface tag.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import generic as generic_handler

pytestmark = pytest.mark.asyncio


async def test_generic_handler_returns_valid_preamble_with_no_meta() -> None:
    """Empty meta still produces a valid preamble with the surface tag."""
    preamble = await generic_handler.build_preamble("w1", "u1", SurfaceMeta())

    assert '<surface kind="generic"' in preamble
    # Route fall-back when no route_path was supplied.
    assert 'route="?"' in preamble
    # And a snapshot block that tells the agent there's no live state.
    assert "no specific surface context" in preamble


async def test_generic_handler_honors_route_path_hint() -> None:
    """When meta carries route_path it is reflected in the surface tag."""
    preamble = await generic_handler.build_preamble(
        "w1", "u1", SurfaceMeta(route_path="/some/new/route")
    )

    assert 'route="/some/new/route"' in preamble
