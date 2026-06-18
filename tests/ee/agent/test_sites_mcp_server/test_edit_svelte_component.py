# tests/ee/agent/test_sites_mcp_server/test_edit_svelte_component.py
# Created: 2026-06-17 (feat/sites-svelte-component-edit, SE-2) — coverage for the
# targeted svelte-component edit tool ``edit_svelte_component`` on the in-process
# ``pocketpaw_sites_manager`` server. Two layers:
#   1. Registration — the tool id rides the SAME server allowlist as publish +
#      create_landing_site + create_svelte_site, and the provider advertises it.
#   2. Handler wiring — identity gating, input validation, the success response
#      shape, and error MAPPING: a SmokeGateFailed from the service becomes an
#      is_error telling the agent the live site is unchanged; a CloudError
#      (NotFound / not-a-svelte-site) is relayed by code. The service-level
#      persist + republish + rollback logic is covered by
#      tests/ee/sites/test_component_edit.py; here we pin the agent surface.
"""Tests for the targeted svelte-component edit tool (edit_svelte_component)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


def _identity(workspace_id: str | None, user_id: str | None):
    """Context managers patching the per-stream identity ContextVar accessors."""
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


class _FakeSiteDoc:
    """Stand-in for the Site Beanie doc the service returns — only the fields the
    handler reads onto the response."""

    def __init__(self) -> None:
        from bson import ObjectId

        self.id = ObjectId()
        self.pocket_id = "pk1"
        self.name = "Bright Smile"
        self.url = "http://127.0.0.1:9999/site/"
        self.deployed = True


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            EDIT_SVELTE_COMPONENT_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert (
            EDIT_SVELTE_COMPONENT_TOOL_ID == "mcp__pocketpaw_sites_manager__edit_svelte_component"
        )
        assert EDIT_SVELTE_COMPONENT_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_edit_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import EDIT_SVELTE_COMPONENT_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert EDIT_SVELTE_COMPONENT_TOOL_ID in CloudSitesMcpProvider().tool_ids()


# ---------------------------------------------------------------------------
# Handler wiring — identity, validation, response shape, error mapping
# ---------------------------------------------------------------------------


class TestEditHandler:
    @pytest.mark.asyncio
    async def test_success_returns_site_and_component_path(self) -> None:
        """A successful edit returns {ok, component_path, site:{...}} and forwards
        the agent's identity + inputs to the service."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        ws_patch, user_patch = _identity("ws1", "u1")
        fake = AsyncMock(return_value=_FakeSiteDoc())
        with (
            ws_patch,
            user_patch,
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_svelte_component",
                new=fake,
            ),
        ):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "new_source": "<section/>",
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["component_path"] == "src/lib/components/Hero.svelte"
        assert body["site"]["pocket_id"] == "pk1"
        assert body["site"]["deployed"] is True
        # The service got the agent identity + the edit inputs.
        fake.assert_awaited_once()
        kwargs = fake.await_args.kwargs
        assert kwargs["workspace_id"] == "ws1"
        assert kwargs["user_id"] == "u1"
        assert kwargs["component_path"] == "src/lib/components/Hero.svelte"
        assert kwargs["new_source"] == "<section/>"

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=(None, None)):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "new_source": "<section/>",
                }
            )
        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_component_path_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_svelte_component_handler(
                {"pocket_id": "pk1", "new_source": "<section/>"}
            )
        assert out.get("is_error") is True
        assert "`component_path`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_non_string_new_source_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "new_source": {"not": "a string"},
                }
            )
        assert out.get("is_error") is True
        assert "`new_source`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_smoke_gate_failure_maps_to_unchanged_site_error(self) -> None:
        """A SmokeGateFailed from the service becomes a clear is_error: the live
        site is unchanged (prior version still deployed). The agent must NOT
        report a successful edit."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.sites.generator_client import SmokeGateFailed

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_svelte_component",
                new=AsyncMock(side_effect=SmokeGateFailed("workerd SSR failure")),
            ),
        ):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "new_source": "<broken/>",
                }
            )
        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "smoke test" in text
        assert "previous version is still deployed" in text

    @pytest.mark.asyncio
    async def test_cloud_error_is_relayed_by_code(self) -> None:
        """A CloudError (e.g. NotFound on the component) is relayed by code +
        message so the agent can tell the user what was wrong."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import NotFound

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_svelte_component",
                new=AsyncMock(
                    side_effect=NotFound("site_component", "src/lib/components/X.svelte")
                ),
            ),
        ):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/X.svelte",
                    "new_source": "<section/>",
                }
            )
        assert out.get("is_error") is True
        assert "site_component.not_found" in out["content"][0]["text"]
