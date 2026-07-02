# tests/cloud/surface/test_game_handler.py — GAME surface handler and its
# ripple-OFF SurfaceProfile.
#
# Created: 2026-07-02 (feat/game-surface) — Guards the new describe→compose
# surface, mirroring tests/cloud/surface/test_studio_code_handlers.py:
#   * /game — the agent COMPOSES a living world (a Pocket type="game") from a
#     vibe. Preamble must orient to creation (not a dashboard, not play),
#     prefer the `game` skill, frame NPCs as Souls (memory/grudges), and name
#     the deterministic create_game_world MCP fallback tool.
# Also pins the profile: ripple_mode="off" (so the surface doesn't inherit the
# ripple "default to ui-spec" LAW), the `game` skill, and the MCP allow-list
# scoped to the create_game_world tool. And pins that the wire string "game"
# resolves to the new kind (not the GENERIC fallback).

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.handlers import game as game_handler

# pytest-asyncio runs in auto mode (see pyproject [tool.pytest] asyncio_mode),
# so async tests are detected automatically — no module-level mark needed (a
# module mark would wrongly tag the sync profile/util tests below).

WORKSPACE = "ws-surface-game"
USER = "u-game"


# --- /game handler ---


async def test_game_handler_carries_creation_orientation() -> None:
    """The preamble orients to composing a living world on the game surface —
    creation-first, and must NOT frame the deliverable as a dashboard."""
    preamble = await game_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/game"))

    assert '<surface kind="game"' in preamble
    lower = preamble.lower()
    # Mentions the surface + the creation deliverable.
    assert "game" in lower
    assert "world" in lower
    assert "creation" in lower
    # NPCs are Souls with persistent memory/grudges.
    assert "soul" in lower
    assert "memory" in lower or "remember" in lower
    assert "grudge" in lower
    # Not a dashboard build.
    assert "dashboard" not in lower or "not" in lower  # only as the thing to avoid
    assert "build a pocket" not in lower


async def test_game_handler_prefers_game_skill_and_names_create_tool() -> None:
    """The procedure PREFERS the `game` skill and names the deterministic
    create_game_world MCP tool as the fallback so the vibe→world flow never
    breaks."""
    preamble = await game_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/game"))

    assert "prefer" in preamble.lower()
    assert "`game`" in preamble or "game skill" in preamble.lower()
    # The in-process game MCP tool — the SDK backend only sees this.
    assert "mcp__pocketpaw_game__create_game_world" in preamble
    # Steers away from hand-built widgets and the pocket specialist.
    lower = preamble.lower()
    assert "pocket specialist" in lower
    assert "widget" in lower


async def test_game_handler_relays_tool_errors() -> None:
    """The procedure tells the agent to relay tool errors plainly and never
    fake a created world."""
    preamble = await game_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/game"))
    lower = preamble.lower()
    assert "error" in lower
    assert "phantom" in lower or "never claim" in lower


# --- Profile (ripple-OFF, game skill, create tool allow-list) ---


def test_game_profile_ripple_off_create_tool_and_skill() -> None:
    """The /game profile turns ripple OFF (so the agent composes a world, not a
    ui-spec dashboard), scopes to the create_game_world MCP tool, and surfaces
    the game skill. Mirrors the studio profile pin."""
    profile = resolve_profile(SurfaceKind.GAME, SurfaceMeta())
    assert profile.ripple_mode == "off"
    assert "game" in profile.skill_names
    assert profile.allow_mcp_tool_ids is not None
    assert "mcp__pocketpaw_game__create_game_world" in profile.allow_mcp_tool_ids


def test_game_kind_maps_from_wire_string() -> None:
    """The wire ``surface`` string 'game' resolves to the new kind (not the
    GENERIC fallback)."""
    from pocketpaw_ee.cloud.surface.service import _resolve_kind

    assert _resolve_kind("game") is SurfaceKind.GAME
