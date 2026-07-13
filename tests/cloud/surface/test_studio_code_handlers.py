# tests/cloud/surface/test_studio_code_handlers.py — STUDIO + CODE surface
# handlers and their ripple-OFF SurfaceProfiles.
#
# Created: 2026-06-10 (feat/studio-code-migration) — Guards the two new
# describe→do surfaces:
#   * /studio — the agent GENERATES media (image + video) and lays it out in a
#     gallery. Preamble must orient to media (not a dashboard), prefer the
#     `studio` skill, and name the media MCP fallback tools.
#   * /code — the agent EDITS + RUNS code. Preamble must orient to coding (not a
#     dashboard), prefer the `code` skill, name the built-in tools, and demand
#     verification before "done".
# Also pins both profiles: ripple_mode="off" (so neither inherits the ripple
# "default to ui-spec" LAW), the per-surface skill, and STUDIO's media-tool
# allow-list / CODE's SDK-tool allow-list.

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.handlers import code as code_handler
from pocketpaw_ee.cloud.surface.handlers import studio as studio_handler

# pytest-asyncio runs in auto mode (see pyproject [tool.pytest] asyncio_mode),
# so async tests are detected automatically — no module-level mark needed (a
# module mark would wrongly tag the sync profile/util tests below).

WORKSPACE = "ws-surface-studiocode"
USER = "u-studiocode"


# --- /studio handler ---


async def test_studio_handler_carries_media_orientation() -> None:
    """The preamble orients to media generation on the studio surface — and must
    NOT frame the deliverable as a dashboard or a pocket."""
    preamble = await studio_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/studio")
    )

    assert '<surface kind="studio"' in preamble
    lower = preamble.lower()
    # Mentions the surface + the media deliverable.
    assert "studio" in lower
    assert "image" in lower
    assert "video" in lower
    assert "gallery" in lower
    # Not a dashboard build.
    assert "dashboard" not in lower or "not" in lower  # only as the thing to avoid
    assert "build a pocket" not in lower


async def test_studio_handler_prefers_studio_skill_and_names_media_tools() -> None:
    """The procedure PREFERS the `studio` skill and names the media MCP tools as
    the fallback so the generate→gallery flow never breaks."""
    preamble = await studio_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/studio")
    )

    assert "prefer" in preamble.lower()
    assert "`studio`" in preamble or "studio skill" in preamble.lower()
    # The in-process media MCP tools — the SDK backend only sees these.
    assert "mcp__pocketpaw_media__image_generate" in preamble
    assert "mcp__pocketpaw_media__video_generate" in preamble


async def test_studio_handler_relays_provider_errors() -> None:
    """The procedure tells the agent to relay provider/key errors plainly and
    never fake a generated asset."""
    preamble = await studio_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/studio")
    )
    lower = preamble.lower()
    assert "error" in lower
    assert "phantom" in lower or "never claim" in lower


# --- /code handler ---


async def test_code_handler_carries_coding_orientation() -> None:
    """The preamble orients to editing + running code on the code surface — and
    must NOT frame the deliverable as a dashboard or a pocket."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))

    assert '<surface kind="code" ' in preamble
    lower = preamble.lower()
    assert "code" in lower
    assert "workspace" in lower
    # Not a dashboard / pocket build.
    assert "build a pocket" not in lower


async def test_code_handler_names_builtin_tools_and_prefers_code_skill() -> None:
    """The procedure names the built-in coding tools and prefers the `code`
    skill (the edit→run→verify loop)."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))

    assert "prefer" in preamble.lower()
    assert "`code`" in preamble or "code skill" in preamble.lower()
    # The built-in tools the SDK backend exposes for coding.
    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep"):
        assert tool in preamble, f"preamble should name the {tool} tool"


async def test_code_handler_demands_verification_and_is_jailed() -> None:
    """The procedure demands verifying (run it / tests) before claiming done, and
    states the workspace is jailed + destructive shell is blocked."""
    preamble = await code_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/code"))
    lower = preamble.lower()
    assert "verify" in lower
    assert "jailed" in lower
    assert "destructive" in lower


# --- Profiles (ripple-OFF on both) ---


def test_studio_profile_ripple_off_media_tools_and_skill() -> None:
    """The /studio profile turns ripple OFF (so the agent generates media, not a
    ui-spec dashboard), scopes to the media MCP tools, and surfaces the studio
    skill."""
    from pocketpaw_ee.agent.mcp_servers.media import (
        IMAGE_GENERATE_TOOL_ID,
        VIDEO_GENERATE_TOOL_ID,
    )

    profile = resolve_profile(SurfaceKind.STUDIO, SurfaceMeta())
    assert profile.ripple_mode == "off"
    assert "studio" in profile.skill_names
    assert profile.allow_mcp_tool_ids is not None
    assert IMAGE_GENERATE_TOOL_ID in profile.allow_mcp_tool_ids
    assert VIDEO_GENERATE_TOOL_ID in profile.allow_mcp_tool_ids


def test_code_profile_ripple_off_sdk_allowlist_and_skill() -> None:
    """The /code profile turns ripple OFF (so the agent edits code, not a
    dashboard), restricts SDK tools to the coding built-ins, and surfaces the
    code skill."""
    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    assert profile.ripple_mode == "off"
    assert "code" in profile.skill_names
    assert profile.allowed_sdk_tools == frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"})
    # Code names no specialized MCP tools — it rides the built-in SDK tools.
    assert profile.allow_mcp_tool_ids is None


def test_studio_and_code_kinds_map_from_wire_strings() -> None:
    """The wire ``surface`` strings 'studio' / 'code' resolve to the new kinds
    (not the GENERIC fallback)."""
    from pocketpaw_ee.cloud.surface.service import _resolve_kind

    assert _resolve_kind("studio") is SurfaceKind.STUDIO
    assert _resolve_kind("code") is SurfaceKind.CODE
