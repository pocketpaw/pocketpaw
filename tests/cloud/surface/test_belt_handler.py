# tests/cloud/surface/test_belt_handler.py — BELT surface handler + its
# ripple-OFF, station-scoped SurfaceProfile.
#
# Created: 2026-06-10 (feat/belt-surface, BS-2 Belt & Pulley stations thin
# slice) — Guards the /belt develop station:
#   * the preamble orients to the station loop (not a dashboard), enforces
#     ORIENT FIRST via loom, names the Instinct gate tool, and states the
#     no-direct-apply / no-phantom-success rules;
#   * the profile pins ripple_mode="off" (so it doesn't inherit the ripple
#     "default to ui-spec" LAW), the exact coding SDK-tool allowlist, the `belt`
#     skill, and an MCP allow-list that contains the 5 loom ids + the gate id;
#   * the wire string "belt" resolves to SurfaceKind.BELT (not GENERIC).
#
# Updated: 2026-06-10 (feat/belt-console-backend, SC-1) — added meta-aware
# preamble tests: with ``meta.repo`` + ``meta.base_branch`` the preamble states
# the bound repo + branch and tells the agent NOT to ask for the repo (and to
# pass exactly those into belt_propose_change); without them it keeps the
# ask-first behavior.
#
# Mirrors test_studio_code_handlers.py. pytest-asyncio runs in auto mode (see
# pyproject [tool.pytest] asyncio_mode), so async tests are detected
# automatically — no module-level mark (it would wrongly tag the sync tests).

from __future__ import annotations

from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile
from pocketpaw_ee.cloud.surface.handlers import belt as belt_handler

WORKSPACE = "ws-surface-belt"
USER = "u-belt"

# The Instinct gate tool id the station proposes its diff through. Literal here
# (and in service.py) until the sibling-branch constant lands.
GATE_TOOL_ID = "mcp__pocketpaw_belt__belt_propose_change"


# --- /belt handler ---


async def test_belt_handler_carries_station_orientation() -> None:
    """The preamble orients to the develop station — and must NOT frame the
    deliverable as a dashboard or a pocket."""
    preamble = await belt_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/belt"))

    assert '<surface kind="belt" ' in preamble
    lower = preamble.lower()
    assert "belt" in lower
    assert "station" in lower
    # The change is proposed through a gate, not applied directly.
    assert "gate" in lower
    assert "diff" in lower
    # Not a dashboard / pocket build.
    assert "build a pocket" not in lower
    assert "ui-spec" not in lower or "not" in lower


async def test_belt_handler_enforces_orient_first() -> None:
    """The procedure makes the agent ORIENT FIRST via loom before touching code."""
    preamble = await belt_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/belt"))

    lower = preamble.lower()
    assert "orient" in lower
    # Names the loom orient tool by id so the agent calls it, not just "look around".
    assert "mcp__loom__orient" in preamble


async def test_belt_handler_names_gate_tool_and_forbids_direct_apply() -> None:
    """The procedure names the Instinct gate tool and forbids applying / pushing /
    merging directly — every change leaves the station ONLY as a proposal."""
    preamble = await belt_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/belt"))

    # The gate tool the agent must propose through.
    assert GATE_TOOL_ID in preamble
    lower = preamble.lower()
    # The no-direct-apply rule.
    assert "never apply" in lower
    assert "directly" in lower
    # And no phantom successes when the gate is unavailable.
    assert "phantom" in lower or "do not claim" in lower


async def test_belt_handler_names_builtin_dev_tools() -> None:
    """The develop stage names the built-in coding tools used in the worktree."""
    preamble = await belt_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/belt"))

    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep"):
        assert tool in preamble, f"preamble should name the {tool} tool"


# --- Meta-aware repo binding (SC-1) ---


async def test_belt_handler_injects_bound_repo_and_branch() -> None:
    """With repo + base_branch in meta, the preamble states them and tells the
    agent NOT to ask for the repo + pass exactly those into the gate tool."""
    meta = SurfaceMeta(
        route_path="/belt",
        repo="/srv/checkouts/acme-api",
        base_branch="develop",
    )
    preamble = await belt_handler.build_preamble(WORKSPACE, USER, meta)

    # The bound repo + branch appear verbatim.
    assert "/srv/checkouts/acme-api" in preamble
    assert "develop" in preamble
    # The agent is told NOT to re-ask for the repo.
    lower = preamble.lower()
    assert "do not ask" in lower or "do not re-ask" in lower or "not ask" in lower
    # The propose instruction names the gate tool with the bound values.
    assert GATE_TOOL_ID in preamble


async def test_belt_handler_ask_first_when_no_repo_bound() -> None:
    """Without repo/base_branch the preamble keeps ask-first behavior — it does
    NOT inject a repo line and instructs the agent to confirm the repo first."""
    preamble = await belt_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/belt"))

    assert "<belt-repo>" not in preamble
    lower = preamble.lower()
    assert "confirm" in lower and "repo" in lower


async def test_belt_handler_partial_meta_is_ask_first() -> None:
    """A repo WITHOUT a base_branch (or vice-versa) is treated as no binding —
    the page must supply BOTH for the bound path."""
    only_repo = await belt_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/belt", repo="/srv/checkouts/acme-api")
    )
    only_branch = await belt_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/belt", base_branch="main")
    )
    assert "<belt-repo>" not in only_repo
    assert "<belt-repo>" not in only_branch


# --- Profile (ripple-OFF, station-scoped) ---


def test_belt_profile_ripple_off_sdk_allowlist_skill_and_mcp_scope() -> None:
    """The /belt profile turns ripple OFF, restricts SDK tools to the coding
    built-ins, surfaces the `belt` skill, and scopes MCP to the loom orientation
    tools + the Instinct gate tool."""
    from pocketpaw_ee.agent.mcp_servers.loom import LOOM_TOOL_IDS

    profile = resolve_profile(SurfaceKind.BELT, SurfaceMeta())

    assert profile.ripple_mode == "off"
    assert "belt" in profile.skill_names
    assert profile.allowed_sdk_tools == frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"})
    # MCP allow-list: the 5 loom orientation tools + the gate tool.
    assert profile.allow_mcp_tool_ids is not None
    assert len(LOOM_TOOL_IDS) == 5
    for loom_id in LOOM_TOOL_IDS:
        assert loom_id in profile.allow_mcp_tool_ids
    assert GATE_TOOL_ID in profile.allow_mcp_tool_ids


def test_belt_kind_maps_from_wire_string() -> None:
    """The wire ``surface`` string 'belt' resolves to SurfaceKind.BELT (not the
    GENERIC fallback)."""
    from pocketpaw_ee.cloud.surface.service import _resolve_kind

    assert _resolve_kind("belt") is SurfaceKind.BELT
