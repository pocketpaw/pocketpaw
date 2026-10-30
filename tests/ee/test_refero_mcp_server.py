# tests/ee/test_refero_mcp_server.py — the Refero design-research MCP surface.
#
# Created 2026-09-15 (feat/refero-design-research).
# Updated 2026-09-27 (feat/sites-visual-research): view_reference joined the
# server; its handler returns image blocks for a screen uuid or a style preview.
#
# The load-bearing test here is REACHABILITY, not registration. /sites runs a
# hard allow-list: a tool can be built, registered on an ambient in-process
# server, and advertised in a skill, and still be silently unreachable because
# its id is absent from that list. So this file asserts the ids survive the
# REAL claude_sdk filter predicate rather than asserting they exist.
#
# The second one is the id/name drift guard. The allow-list is a set of
# STRINGS; the server's tools are decorated functions. Nothing connects them at
# import time, so renaming a tool without renaming its constant produces a tool
# nobody can call and an allow-list entry that matches nothing — with no error
# anywhere.

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.agent.mcp_servers import refero as refero_mcp
from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile


def _survives_sites_filter(tool_id: str) -> bool:
    """Mirror claude_sdk's non-exclusive allow-filter predicate exactly."""
    from pocketpaw.agents.claude_sdk import (
        ALWAYS_ALLOWED_MCP_SERVERS,
        POCKET_CREATION_GRANT,
        _mcp_server_of,
    )
    from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_studio import STUDIO_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

    allow = resolve_profile(SurfaceKind.SITES, SurfaceMeta()).allow_mcp_tool_ids
    assert allow is not None, "/sites must keep a restrictive allow-list"
    grant = (
        allow
        | POCKET_CREATION_GRANT
        | frozenset(WIDGET_TOOL_IDS)
        | frozenset(ATLAS_TOOL_IDS)
        | frozenset(STUDIO_TOOL_IDS)
    )
    return (
        not tool_id.startswith("mcp__")
        or tool_id in grant
        or _mcp_server_of(tool_id) in ALWAYS_ALLOWED_MCP_SERVERS
    )


def test_design_research_is_reachable_on_every_sites_mode():
    """Present in the allow set is not the claim — surviving the filter is.

    Checked on all three /sites modes because they resolve through different
    branches of ``_sites_profile``, and a grant that only reached ripple-create
    would leave the svelte/react authoring path — the one whose skill actually
    talks about design systems — without it.
    """
    for meta in (SurfaceMeta(), SurfaceMeta(engine="svelte"), SurfaceMeta(engine="react")):
        profile = resolve_profile(SurfaceKind.SITES, meta)
        allow = profile.allow_mcp_tool_ids
        assert allow is not None
        for tool_id in refero_mcp.REFERO_TOOL_IDS:
            assert tool_id in allow, f"{tool_id} missing from /sites allow set for {meta!r}"
            assert tool_id not in profile.deny_mcp_tool_ids

    for tool_id in refero_mcp.REFERO_TOOL_IDS:
        assert _survives_sites_filter(tool_id), f"{tool_id} would be filtered out on /sites"

    # Control: the allow-list is real, not a no-op that keeps everything.
    assert not _survives_sites_filter("mcp__pocketpaw_foresight__save_scenario")


def test_tool_ids_match_the_tools_the_server_actually_builds():
    """A constant that names no real tool is an allow-list entry matching nothing.

    Nothing links the id strings to the decorated functions at import time, so
    this is the only thing standing between a rename and a silently uncallable
    tool.
    """
    built = refero_mcp.build_refero_server()
    if built is None:
        pytest.skip("claude_agent_sdk not installed")
    name, _server = built
    assert name == refero_mcp.SERVER_NAME

    declared = {tid.split("__")[-1] for tid in refero_mcp.REFERO_TOOL_IDS}
    assert declared == {"search_styles", "get_style", "search_screens", "view_reference"}
    # Every id must namespace under this server, or the filter's server check
    # and the allow-list disagree about who owns the tool.
    for tid in refero_mcp.REFERO_TOOL_IDS:
        assert tid.startswith(f"mcp__{refero_mcp.SERVER_NAME}__")


def test_the_provider_advertises_exactly_the_servers_ids():
    """The entry-point provider is what puts the ids on the SDK allow-list."""
    from pocketpaw_ee.extensions import CloudReferoMcpProvider

    assert CloudReferoMcpProvider().tool_ids() == list(refero_mcp.REFERO_TOOL_IDS)


# ── handlers ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_blank_query_is_refused_without_calling_upstream():
    """A blank query must not spend a call against the monthly quota."""
    for handler in (refero_mcp._search_styles_handler, refero_mcp._search_screens_handler):
        out = await handler({"query": "   "})
        assert out.get("is_error") is True
    out = await refero_mcp._get_style_handler({})
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_styles_search_returns_the_helper_result(monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.tools.builtin.refero.search_styles",
        lambda q, n: [{"uuid": "u1", "title": "Depot"}],
    )
    out = await refero_mcp._search_styles_handler({"query": "developer tool"})

    assert out.get("is_error") is not True
    body = json.loads(out["content"][0]["text"])
    assert body == {"ok": True, "count": 1, "results": [{"uuid": "u1", "title": "Depot"}]}


@pytest.mark.asyncio
async def test_an_unconfigured_refero_is_an_empty_result_not_an_error():
    """Refero needs a paid plan, so unconfigured is the COMMON case.

    It has to look like "nothing matched", because an ``is_error`` response
    makes the agent retry or apologise mid-build instead of just designing.
    """
    out = await refero_mcp._search_styles_handler({"query": "anything"})

    assert out.get("is_error") is not True
    body = json.loads(out["content"][0]["text"])
    assert body["ok"] is True
    assert body["results"] == []


@pytest.mark.asyncio
async def test_an_upstream_failure_is_reported_not_raised(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("refero exploded")

    monkeypatch.setattr("pocketpaw.tools.builtin.refero.search_screens", _boom)
    out = await refero_mcp._search_screens_handler({"query": "pricing page"})

    assert out.get("is_error") is True
    assert "refero exploded" in out["content"][0]["text"]


# ── view_reference ──────────────────────────────────────────────────────────


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_view_reference_returns_the_screen_as_images(monkeypatch):
    seen: list[tuple] = []

    def _image(screen_id, size):
        seen.append((screen_id, size))
        return _png()

    monkeypatch.setattr("pocketpaw.tools.builtin.refero.get_screen_image", _image)
    out = await refero_mcp._view_reference_handler({"screen_id": "s-1"})

    assert out.get("is_error") is not True
    kinds = [part["type"] for part in out["content"]]
    assert kinds[0] == "image" and kinds[-1] == "text"
    assert seen == [("s-1", "full")]


@pytest.mark.asyncio
async def test_view_reference_refuses_a_preview_url_off_refero(monkeypatch):
    """The URL is agent-supplied, so it must never become a fetch of anywhere."""
    out = await refero_mcp._view_reference_handler({"preview_url": "https://evil.com/x.jpg"})
    assert out.get("is_error") is True
    assert "evil.com" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_view_reference_needs_a_screen_or_a_preview():
    out = await refero_mcp._view_reference_handler({})
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_a_missing_screenshot_is_an_error_that_says_carry_on(monkeypatch):
    monkeypatch.setattr("pocketpaw.tools.builtin.refero.get_screen_image", lambda *_a: None)
    out = await refero_mcp._view_reference_handler({"screen_id": "gone"})
    assert out.get("is_error") is True
    assert "carry on" in out["content"][0]["text"].lower()
