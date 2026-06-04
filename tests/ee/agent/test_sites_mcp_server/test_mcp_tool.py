# tests/ee/agent/test_sites_mcp_server/test_mcp_tool.py
# Created: 2026-06-01 (Phase 4 — chat→create-site) — coverage for the in-process
# ``pocketpaw_sites_manager`` MCP server. Mirrors the foresight/pocket_specialist
# test layout: registration assertions (server name, tool id, build, provider
# allowlist publication) plus per-handler tests that mock the identity
# ContextVars + the shared publish_pocket service and inspect the MCP envelope
# the SDK returns to the agent.
"""MCP server registration + handler tests for the Paw Sites publish tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestSitesMcpServerRegistration:
    def test_server_name_and_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            CREATE_LANDING_SITE_TOOL_ID,
            CREATE_SVELTE_SITE_TOOL_ID,
            PUBLISH_TOOL_ID,
            SERVER_NAME,
            SITES_TOOL_IDS,
        )

        assert SERVER_NAME == "pocketpaw_sites_manager"
        # Allowlist entries must use the exact ``mcp__<server>__<tool>`` form.
        assert PUBLISH_TOOL_ID == "mcp__pocketpaw_sites_manager__publish"
        # The deterministic create tools register on the SAME server (two
        # create_sdk_mcp_server calls under one name would clobber each other),
        # so all ids ride the one ``pocketpaw_sites_manager`` server: the ripple
        # landing tool + the svelte-track tool sit beside publish.
        assert CREATE_LANDING_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__create_landing_site"
        assert CREATE_SVELTE_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__create_svelte_site"
        assert PUBLISH_TOOL_ID in SITES_TOOL_IDS
        assert CREATE_LANDING_SITE_TOOL_ID in SITES_TOOL_IDS
        assert CREATE_SVELTE_SITE_TOOL_ID in SITES_TOOL_IDS
        assert len(SITES_TOOL_IDS) == 3

    def test_extension_provider_advertises_tool_id(self) -> None:
        """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
        allowlist loop — the publish tool id must come through it."""
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        advertised = CloudSitesMcpProvider().tool_ids()
        for tid in SITES_TOOL_IDS:
            assert tid in advertised

    def test_provider_build_server_matches_shape(self) -> None:
        """The provider's ``build_server`` returns ``(name, server)`` when the
        Claude Agent SDK is installed (the ee group), or ``None`` otherwise."""
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        out = CloudSitesMcpProvider().build_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_sites_manager"
            assert server is not None

    def test_build_server_returns_object(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        out = build_sites_manager_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_sites_manager"
            assert server is not None

    def test_provider_is_ambient_not_opt_in(self) -> None:
        """The sites server must NOT be opt-in — otherwise the bundled skill
        couldn't reach it without an explicit per-agent opt-in. This guards the
        ambient regime the chat→create-site flow depends on."""
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

        assert "pocketpaw_sites_manager" not in OPT_IN_MCP_SERVERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payload(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``. Decode it so
    the tests can assert on dict fields without re-encoding."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


class _FakeSiteDoc:
    """Minimal stand-in for the Beanie Site doc the service returns — only the
    fields the handler reads."""

    def __init__(self) -> None:
        self.id = "site_abc123"
        self.pocket_id = "pk_1"
        self.name = "Bright Smile Dental"
        self.url = "http://localhost:8899/site_abc123/"
        self.deployed = True


def _patch_identity(workspace_id: str | None, user_id: str | None):
    """Patch the per-stream identity accessors the handler reads via
    ``_identity`` (imported function-locally from agent_service)."""
    return (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=workspace_id,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            return_value=user_id,
        ),
    )


# ---------------------------------------------------------------------------
# Handler — publish
# ---------------------------------------------------------------------------


class TestPublishHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_site_with_url(self) -> None:
        """Given a pocket_id + ContextVar identity, the handler delegates to
        publish_pocket and returns the site (with its openable url)."""
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

        ws_patch, user_patch = _patch_identity("ws_1", "u_1")
        with (
            ws_patch,
            user_patch,
            patch(
                "pocketpaw_ee.sites.service.publish_pocket",
                new=AsyncMock(return_value=_FakeSiteDoc()),
            ) as mock_publish,
        ):
            out = await sites_mcp._publish_handler(
                {"pocket_id": "pk_1", "name": "Bright Smile Dental"}
            )

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["ok"] is True
        assert body["site"]["id"] == "site_abc123"
        assert body["site"]["url"] == "http://localhost:8899/site_abc123/"
        assert body["site"]["deployed"] is True
        assert body["site"]["pocket_id"] == "pk_1"
        # Identity flows from the ContextVars, not the args.
        mock_publish.assert_awaited_once_with(
            workspace_id="ws_1", user_id="u_1", pocket_id="pk_1", name="Bright Smile Dental"
        )

    @pytest.mark.asyncio
    async def test_name_defaults_to_empty_when_omitted(self) -> None:
        """``name`` is optional — omitted, the handler passes "" so the service
        falls back to the pocket's own name."""
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

        ws_patch, user_patch = _patch_identity("ws_1", "u_1")
        with (
            ws_patch,
            user_patch,
            patch(
                "pocketpaw_ee.sites.service.publish_pocket",
                new=AsyncMock(return_value=_FakeSiteDoc()),
            ) as mock_publish,
        ):
            out = await sites_mcp._publish_handler({"pocket_id": "pk_1"})

        assert not out.get("is_error")
        mock_publish.assert_awaited_once_with(
            workspace_id="ws_1", user_id="u_1", pocket_id="pk_1", name=""
        )

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        """No workspace/user context (called outside an SSE chat stream) → the
        handler returns is_error without touching the service."""
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

        ws_patch, user_patch = _patch_identity(None, None)
        with (
            ws_patch,
            user_patch,
            patch(
                "pocketpaw_ee.sites.service.publish_pocket",
                new=AsyncMock(),
            ) as mock_publish,
        ):
            out = await sites_mcp._publish_handler({"pocket_id": "pk_1"})

        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]
        mock_publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_pocket_id_is_error(self) -> None:
        """A blank / missing pocket_id is rejected before the service is called."""
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

        ws_patch, user_patch = _patch_identity("ws_1", "u_1")
        with (
            ws_patch,
            user_patch,
            patch(
                "pocketpaw_ee.sites.service.publish_pocket",
                new=AsyncMock(),
            ) as mock_publish,
        ):
            out = await sites_mcp._publish_handler({})

        assert out.get("is_error") is True
        assert "pocket_id" in out["content"][0]["text"]
        mock_publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pocket_not_found_surfaces_as_is_error(self) -> None:
        """When the pockets service raises NotFound (a CloudError), the handler
        relays the code/message as is_error — no phantom 'published' reply."""
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp
        from pocketpaw_ee.cloud._core.errors import NotFound

        ws_patch, user_patch = _patch_identity("ws_1", "u_1")
        with (
            ws_patch,
            user_patch,
            patch(
                "pocketpaw_ee.sites.service.publish_pocket",
                new=AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
            ),
        ):
            out = await sites_mcp._publish_handler({"pocket_id": "pk_missing"})

        assert out.get("is_error") is True
        # The CloudError code is surfaced so the agent can tell the user.
        assert "not_found" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_build_failure_surfaces_as_is_error(self) -> None:
        """A non-CloudError failure (e.g. the smoke gate / deploy) is caught and
        surfaced as is_error rather than crashing the tool call."""
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

        ws_patch, user_patch = _patch_identity("ws_1", "u_1")
        with (
            ws_patch,
            user_patch,
            patch(
                "pocketpaw_ee.sites.service.publish_pocket",
                new=AsyncMock(side_effect=RuntimeError("workerd smoke render failed")),
            ),
        ):
            out = await sites_mcp._publish_handler({"pocket_id": "pk_1"})

        assert out.get("is_error") is True
        assert "publish failed" in out["content"][0]["text"]
