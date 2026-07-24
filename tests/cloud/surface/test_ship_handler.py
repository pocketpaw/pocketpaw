# tests/cloud/surface/test_ship_handler.py — SHIP surface handler + its
# ripple-OFF, ship-scoped SurfaceProfile.
#
# Created: 2026-07-23 (feat/ship-surface-kind, SHIP-8a) — Guards the /ship
# managed-deploy control plane:
#   * the preamble orients to the control plane (not a dashboard/pocket), names
#     the ``mcp__pocketpaw_ship__*`` verb tools, and — load-bearing — states THE
#     SAFETY RULE: tearing anything down only FILES A PROPOSAL in The Tray, so
#     the agent must never claim something was destroyed (returns "proposed"),
#     and never claim a deploy/provision succeeded without an ok (no phantom
#     successes);
#   * the profile pins ripple_mode="off" (so it doesn't inherit the ripple
#     "default to ui-spec" LAW), surfaces the `ship` skill, and scopes
#     ``allow_mcp_tool_ids`` to the ship verb tools — degrading to None (no MCP
#     restriction) when the EE agent-layer tool-id import fails;
#   * the wire string "ship" resolves to SurfaceKind.SHIP (not GENERIC).
#
# Mirrors test_belt_handler.py. pytest-asyncio runs in auto mode (see pyproject
# [tool.pytest] asyncio_mode), so async tests are detected automatically — no
# module-level mark (it would wrongly tag the sync profile/registry tests).

from __future__ import annotations

from pocketpaw_ee.agent.mcp_servers.ship import SHIP_TOOL_IDS
from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.handlers import ship as ship_handler

WORKSPACE = "ws-surface-ship"
USER = "u-ship"

# The teardown-proposal verb — the load-bearing tool of the safety rule.
REQUEST_DESTROY_TOOL_ID = "mcp__pocketpaw_ship__ship_request_destroy"


# --- /ship handler ---


async def test_ship_handler_carries_control_plane_orientation() -> None:
    """The preamble orients to the managed-deploy control plane — and must NOT
    frame the deliverable as a dashboard or a pocket."""
    preamble = await ship_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/ship"))

    assert '<surface kind="ship" ' in preamble
    lower = preamble.lower()
    assert "ship" in lower
    assert "control plane" in lower
    # The /ship vocabulary — boxes, apps, deploys, the Tray.
    assert "box" in lower
    assert "app" in lower
    assert "deploy" in lower
    assert "tray" in lower
    # Not a dashboard / pocket build.
    assert "build a pocket" not in lower
    assert "do not create a pocket" in lower
    assert "ui-spec" not in lower or "not" in lower


async def test_ship_handler_names_all_ship_verb_tools() -> None:
    """The procedure names every ``mcp__pocketpaw_ship__*`` verb so the agent
    drives managed deploys through them (not through code or a dashboard)."""
    preamble = await ship_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/ship"))

    assert "mcp__pocketpaw_ship__" in preamble
    for tool_id in SHIP_TOOL_IDS:
        assert tool_id in preamble, f"preamble should name the {tool_id} verb"


async def test_ship_handler_teardown_only_proposes() -> None:
    """THE SAFETY RULE: tearing anything down only FILES A PROPOSAL — the agent
    calls request-destroy, which returns 'proposed', and NEVER claims something
    was destroyed. This is the load-bearing acceptance test."""
    preamble = await ship_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/ship"))

    # Names the teardown-proposal tool.
    assert REQUEST_DESTROY_TOOL_ID in preamble
    lower = preamble.lower()
    # The proposal → Tray → human-approval flow.
    assert "proposed" in lower
    assert "proposal" in lower
    assert "tray" in lower
    assert "approv" in lower  # approve / approval
    # The honesty rule: never claim a destroy that only proposed.
    assert "never" in lower
    assert "destroyed" in lower
    # A prod deploy is gated the same way.
    assert "production" in lower


async def test_ship_handler_forbids_phantom_successes() -> None:
    """The procedure forbids phantom successes — a tool returning ok means the
    work was ACCEPTED, not finished, so the agent never claims a deploy/provision
    succeeded unless the tool actually returned ok."""
    preamble = await ship_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/ship"))

    lower = preamble.lower()
    assert "phantom" in lower or "never claim" in lower
    # Backgrounded work must be polled, not assumed.
    assert "background" in lower
    assert "poll" in lower


# --- Profile (ripple-OFF, ship-scoped) ---


def test_ship_profile_ripple_off_scoped_to_ship_tools_and_skill() -> None:
    """The /ship profile turns ripple OFF (so the agent drives deploys, not a
    dashboard), surfaces the `ship` skill, and scopes MCP to the ship verb
    tools."""
    profile = resolve_profile(SurfaceKind.SHIP, SurfaceMeta())

    assert profile.ripple_mode == "off"
    assert "ship" in profile.skill_names
    # MCP allow-list: exactly the 16 ship verb tools (Wave 2 added set_scale +
    # set_checks; Wave 3 added set_resources + create_volume + restart + rebuild).
    assert profile.allow_mcp_tool_ids is not None
    assert len(SHIP_TOOL_IDS) == 16
    for tool_id in SHIP_TOOL_IDS:
        assert tool_id in profile.allow_mcp_tool_ids
    # Ship drives infra through MCP verbs, not code — no SDK-tool allowlist.
    assert profile.allowed_sdk_tools is None


def test_ship_profile_degrades_to_no_restriction_on_import_failure(monkeypatch) -> None:
    """If the EE agent-layer tool-id import fails, the loader degrades every
    allow-list to None (no MCP restriction) so tool-scoping can NEVER break
    chat. SHIP rides that same path: ripple stays OFF, but the allow-list drops
    to None rather than filtering every MCP tool out."""
    from pocketpaw_ee.cloud.surface import surface_registry as reg

    degraded = reg._McpToolIds(
        loaded=False,
        foresight_allow=None,
        sites_allow=None,
        studio_allow=None,
        belt_allow=None,
        ship_allow=None,
    )
    monkeypatch.setattr(reg, "_MCP_TOOL_IDS_CACHE", degraded)

    profile = resolve_profile(SurfaceKind.SHIP, SurfaceMeta())
    assert profile.ripple_mode == "off"  # ripple stays off regardless
    assert profile.allow_mcp_tool_ids is None  # degraded → no restriction


# --- Registry + wire mapping ---


def test_ship_registered_once_with_ship_route() -> None:
    """The registry has EXACTLY ONE SHIP row and it resolves to '/ship'."""
    from pocketpaw_ee.cloud.surface.surface_registry import SURFACES, _route_for

    ship_rows = [s for s in SURFACES if s.kind is SurfaceKind.SHIP]
    assert len(ship_rows) == 1
    assert ship_rows[0].route == "/ship"
    assert _route_for(SurfaceKind.SHIP) == "/ship"


def test_ship_kind_maps_from_wire_string() -> None:
    """The wire ``surface`` string 'ship' resolves to SurfaceKind.SHIP (not the
    GENERIC fallback)."""
    from pocketpaw_ee.cloud.surface.service import _resolve_kind

    assert _resolve_kind("ship") is SurfaceKind.SHIP
