# test_timeline_reachability.py — can the agent actually CALL the timeline tools?
#
# Created: 2026-09-08 (feat/agentic-studio-editor), after a live /studio/editor
# session where the preamble worked perfectly — the agent listed the user's
# clips with correct id tails and asked sensible questions — and then answered
# every edit request with "I don't have access to the timeline editing tool".
#
# The unit tests were all green. They covered the validator, the handlers, the
# applier and the preamble, and not one of them asked the only question that
# decides whether the feature exists: does the tool reach the agent?
#
# It did not. The provider was written and the entry point was added to
# ee/pyproject.toml, but discovery reads INSTALLED metadata, so an editable
# install that had not been re-synced served a stale entry-point table. Exactly
# the failure the #FU-F comment in claude_sdk.py warns about ("a stale editable
# install + dashboard restart ... the diagnostic took 30+ minutes because the
# failure mode was invisible").
#
# test_mcp_provider_registry.py already had a guard that would have caught it —
# it diffs McpProvider classes against registered entry points. That guard never
# ran, because the module failed at COLLECTION on an unrelated stale entry
# (design_systems). A green run and a run that never happened look identical in
# a summary line.
#
# So these tests walk the real chain, cheaply, with no mocks: registered →
# builds → ids → allowed on the surface. If any link breaks, the feature is
# dead, and it must fail here rather than in front of a user.

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from pocketpaw_ee.agent.mcp_servers.timeline import (
    SERVER_NAME,
    TIMELINE_TOOL_IDS,
)
from pocketpaw_ee.cloud.surface import service
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta

from pocketpaw._registry import providers
from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

_GROUP = "pocketpaw.mcp_servers"


def test_the_entry_point_is_registered() -> None:
    """Link 1: discovery is ONLY via installed entry-point metadata.

    A provider class that exists in source but not in the installed table is
    invisible to every backend — which is precisely how this shipped broken.
    A failure here usually means `uv pip install -e ee` has not been re-run
    since ee/pyproject.toml changed.
    """
    names = {ep.name for ep in entry_points(group=_GROUP)}
    assert "timeline" in names, (
        "the timeline MCP provider is not in the installed entry-point table — "
        "the agent cannot see edit_timeline. Re-sync the editable install: "
        "uv pip install -e ee --no-deps"
    )


def test_the_server_actually_builds() -> None:
    """Link 2: a provider that raises or returns None registers no tools.

    build_server() failures are caught and logged by the SDK backend, so a
    broken one degrades to silence rather than an error.
    """
    built = [
        p.build_server()
        for p in providers(_GROUP)
        if type(p).__name__ == "CloudTimelineMcpProvider"
    ]
    assert built, "CloudTimelineMcpProvider is not discoverable through the registry"
    assert built[0] is not None, (
        "build_server() returned None — claude_agent_sdk is missing, so the "
        "server registers no tools"
    )
    assert built[0][0] == SERVER_NAME


def test_the_tools_are_ambient_not_opt_in() -> None:
    """Link 3: an OPT_IN server needs the agent to name it in its tools list.

    The timeline tools refuse on their own when no editor is open, so they are
    deliberately ambient. If they ever land in OPT_IN_MCP_SERVERS they go
    silently missing on every surface that does not opt in.
    """
    assert SERVER_NAME not in OPT_IN_MCP_SERVERS


@pytest.mark.parametrize("tool_id", TIMELINE_TOOL_IDS)
def test_the_tool_is_reachable_on_the_studio_editor_surface(tool_id: str) -> None:
    """Link 4: built is not the same as allowed.

    /studio/editor uses a RESTRICTIVE allow-list, so a tool absent from it is
    stripped from the run even though the server built fine.
    """
    profile = service.resolve_profile(SurfaceKind.STUDIO_EDITOR, SurfaceMeta())
    allow = set(profile.allow_mcp_tool_ids or [])
    deny = set(profile.deny_mcp_tool_ids or [])
    assert tool_id in allow, f"{tool_id} is not allowed on /studio/editor"
    assert tool_id not in deny, f"{tool_id} is denied on /studio/editor"


def test_provider_tool_ids_match_the_server_module() -> None:
    """The allowlist is built from tool_ids(); a typo there silently strips the
    tool while every other check still passes."""
    provider = next(p for p in providers(_GROUP) if type(p).__name__ == "CloudTimelineMcpProvider")
    assert set(provider.tool_ids()) == set(TIMELINE_TOOL_IDS)


def test_the_preamble_only_names_tools_that_are_reachable() -> None:
    """The prompt may not command a tool the agent does not have.

    pocketpaw/CLAUDE.md: a model handed an unsatisfiable instruction does not
    raise, it improvises — which is exactly what the live session did, inventing
    a Skill call for `mcp__pocketpaw_timeline__edit_timeline` and then telling
    the user to edit by hand.
    """
    import asyncio
    import re

    from pocketpaw_ee.cloud.surface.handlers import studio_editor

    text = asyncio.run(
        studio_editor.build_preamble(
            "w",
            "u",
            SurfaceMeta(route_path="/studio/editor", timeline={"name": "t", "clips": []}),
        )
    ).text

    named = set(re.findall(r"mcp__[a-z_]+__[a-z_]+", text))
    assert named, "the preamble names no tools at all — the procedure block is missing"

    profile = service.resolve_profile(SurfaceKind.STUDIO_EDITOR, SurfaceMeta())
    allow = set(profile.allow_mcp_tool_ids or [])
    unreachable = named - allow
    assert not unreachable, (
        f"the /studio/editor preamble names tools the agent cannot call: {sorted(unreachable)}"
    )
