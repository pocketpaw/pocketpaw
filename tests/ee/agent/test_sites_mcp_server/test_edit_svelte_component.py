# tests/ee/agent/test_sites_mcp_server/test_edit_svelte_component.py
# Created: 2026-06-17 (feat/sites-svelte-component-edit, SE-2) — coverage for the
# targeted svelte-component edit tool ``edit_svelte_component`` on the in-process
# ``pocketpaw_sites_manager`` server. Two layers:
#   1. Registration — the tool id rides the SAME server allowlist as publish +
#      create_landing_site + create_svelte_site, and the provider advertises it.
#   2. Handler wiring — identity gating, input validation, the success response
#      shape, and error MAPPING: a SmokeGateFailed from the service becomes an
#      is_error telling the agent the edit was not staged; a CloudError
#      (NotFound / not-a-svelte-site) is relayed by code. The service-level
#      persist + republish + rollback logic is covered by
#      tests/ee/sites/test_component_edit.py; here we pin the agent surface.
#
# Updated: 2026-06-18 (fix/sites-edit-draft-not-publish, BUG 2) — an edit now stages
# a DRAFT PREVIEW, not a live publish. The handler returns deployed=False +
# status="draft" + is_live=False + a preview_url + a message that says draft/preview
# and "Submit for review"; it must NOT narrate published/republished/live. The
# success-shape test asserts the draft/preview framing; the smoke-gate test asserts
# the "previous version is unchanged" (not "...still deployed") wording.
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
    """Stand-in for the Site doc the service returns — only the fields the handler
    reads onto the response. The edit path returns a PREVIEW (deployed=False) since
    an edit is now staged as a draft, not deployed live."""

    def __init__(self) -> None:
        from bson import ObjectId

        self.id = ObjectId()
        self.pocket_id = "pk1"
        self.name = "Bright Smile"
        self.url = "http://127.0.0.1:9999/preview-pk1/"
        # An edit builds a PREVIEW, not a live deploy.
        self.deployed = False


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
        # The edit returns a PREVIEW, not a live deploy.
        assert body["site"]["deployed"] is False
        # The service got the agent identity + the edit inputs.
        fake.assert_awaited_once()
        kwargs = fake.await_args.kwargs
        assert kwargs["workspace_id"] == "ws1"
        assert kwargs["user_id"] == "u1"
        assert kwargs["component_path"] == "src/lib/components/Hero.svelte"
        assert kwargs["new_source"] == "<section/>"

    @pytest.mark.asyncio
    async def test_success_payload_says_draft_preview_not_published(self) -> None:
        """BUG 2: the edit success result must tell the agent the change is staged
        as a DRAFT PREVIEW (NOT live/published) and that the user must Submit for
        review to publish. The agent narrates this payload, so it must not claim
        'published'/'live'/'republished'."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_svelte_component",
                new=AsyncMock(return_value=_FakeSiteDoc()),
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

        # The payload carries a draft/preview framing the agent relays.
        assert body["status"] == "draft"
        assert body["is_live"] is False
        assert "preview_url" in body["site"]
        # A human-readable message that says preview/draft + Submit for review.
        message = (body.get("message") or "").lower()
        assert "preview" in message or "draft" in message
        assert "submit for review" in message

        # And it does NOT CLAIM the edit is published / republished / live. The
        # word "publish" may appear as an instruction ("to take it live, click
        # Submit for review") but never as a completed-state claim about THIS edit.
        text = out["content"][0]["text"].lower()
        assert "republished" not in text
        # No "published"/"is published"/"now published" completed-state claim.
        assert "published" not in text
        # No "live at <url>" / "now live" / "is live" publish claim. (is_live is a
        # JSON key with an underscore, so the bare phrase "is live" must be absent.)
        assert "live at" not in text
        assert "now live" not in text
        assert "is live" not in text

    @pytest.mark.asyncio
    async def test_edits_input_forwarded_to_service(self) -> None:
        """P3: the agent can send a targeted ``edits`` list INSTEAD of the whole
        ``new_source``; the handler forwards the blocks to the service and does NOT
        require ``new_source``."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_FakeSiteDoc())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_svelte_component", new=fake),
        ):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "edits": [{"old_string": "Bright", "new_string": "Brighter"}],
                }
            )

        assert not out.get("is_error"), out
        fake.assert_awaited_once()
        kwargs = fake.await_args.kwargs
        assert kwargs["edits"] == [{"old_string": "Bright", "new_string": "Brighter"}]
        # No full-file rewrite was forced on the diff path.
        assert kwargs.get("new_source") is None

    @pytest.mark.asyncio
    async def test_neither_edits_nor_new_source_is_error(self) -> None:
        """P3: a call with neither ``edits`` nor ``new_source`` is rejected with a
        clear is_error before the service is touched."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                }
            )
        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "edits" in text and "new_source" in text

    @pytest.mark.asyncio
    async def test_malformed_edits_is_error(self) -> None:
        """P3: ``edits`` must be a list of {old_string, new_string} dicts — a
        malformed shape is caught at the handler with a clear is_error."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "edits": [{"old_string": "only-old"}],
                }
            )
        assert out.get("is_error") is True
        assert "`edits`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_validation_error_from_apply_relayed(self) -> None:
        """P3: when the service's apply step rejects the edit (e.g. old_string does
        not match uniquely) the ValidationError is relayed by code + message so the
        agent can retry with a more specific old_string."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import ValidationError

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_svelte_component",
                new=AsyncMock(
                    side_effect=ValidationError(
                        "site_edit.no_match",
                        "old_string did not match (0 times)",
                    )
                ),
            ),
        ):
            out = await mcp._edit_svelte_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/lib/components/Hero.svelte",
                    "edits": [{"old_string": "nope", "new_string": "x"}],
                }
            )
        assert out.get("is_error") is True
        assert "site_edit.no_match" in out["content"][0]["text"]

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
        # The edit stages a draft preview, so a smoke failure means it was NOT
        # staged and the previous version is unchanged (no deploy to roll back to).
        assert "previous version is unchanged" in text

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
