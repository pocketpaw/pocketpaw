# tests/ee/agent/test_sites_mcp_server/test_tool_guidance.py
# Created: 2026-09-24 (docs/sites-packages-and-verify-guidance, PP-3) — pins the
# agent-facing prose on the ``pocketpaw_sites_manager`` tools after npm packages
# (PP-1) and the verify pipeline (PP-2) shipped:
#   1. no create/edit description still claims a site cannot take a dependency;
#      each one names ``set_site_dependencies`` instead;
#   2. every create/edit description carries the verification contract: only
#      ``passed`` means ready, ``failed`` means fix and call ``verify_site``.
"""Tool-description guidance for packages and verification on the sites server."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pocketpaw_ee")

SOURCE_TOOLS = (
    "create_svelte_site",
    "create_react_site",
    "create_html_site",
    "edit_svelte_component",
    "edit_react_component",
    "edit_html_file",
)

STALE_CLAIMS = (
    "no way to add a dependency",
    "there is no way to add",
    "nothing else",
    "cannot add a dependency",
    "no npm",
)


def _descriptions() -> dict[str, str]:
    from mcp import types
    from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

    built = build_sites_manager_server()
    if built is None:
        pytest.skip("claude_agent_sdk not installed")
    _name, server = built
    handler = server["instance"].request_handlers[types.ListToolsRequest]
    listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    return {t.name: t.description or "" for t in listed.root.tools}


@pytest.mark.parametrize("name", SOURCE_TOOLS)
def test_no_stale_no_dependency_claim(name: str) -> None:
    """Packages are declarable now; a description that says otherwise sends the
    agent into hand-rolling a library or telling the user it cannot be done."""
    description = _descriptions()[name].lower()
    for claim in STALE_CLAIMS:
        assert claim not in description, f"{name} still says {claim!r}"
    assert "set_site_dependencies" in description


@pytest.mark.parametrize("name", SOURCE_TOOLS)
def test_carries_the_verification_contract(name: str) -> None:
    """The agent must not call a draft ready on a failed or unchecked build."""
    description = _descriptions()[name]
    assert "verification.status" in description
    assert "passed" in description
    assert "verify_site" in description
    assert "unverified" in description
