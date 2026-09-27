# tests/ee/agent/test_sites_mcp_server/test_preview_site.py — the agent looks at its
# own draft.
#
# Created 2026-09-27 (feat/sites-visual-research). ``preview_site`` photographs the
# current draft with Browser Rendering and returns image blocks. These tests pin the
# three answers it can give: pictures, "not ready yet, verify first", and "no
# screenshots on this deployment" — and that none of them stores anything or claims
# the site is broken.
from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from PIL import Image  # noqa: E402


def _png(width: int = 1280, height: int = 3000) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeCF:
    def __init__(self, image: bytes = b"") -> None:
        self.image = image or _png()
        self.calls: list[dict[str, Any]] = []

    async def capture_screenshot(self, **kwargs: Any) -> bytes:
        self.calls.append(kwargs)
        return self.image


@pytest.fixture
def ctx():
    """Identity, an open plan gate and a readable pocket, without a database."""
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
        patch(
            "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
            new=AsyncMock(return_value="go"),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "engine": "html", "source": {}}),
        ),
    ):
        yield


async def test_it_returns_the_draft_as_images(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with (
        patch(
            "pocketpaw_ee.sites.draft_markup.build_draft_markup",
            new=AsyncMock(return_value="<html><body>hi</body></html>"),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", return_value=cf),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1", "device": "mobile"})

    assert out.get("is_error") is not True, out
    kinds = [part["type"] for part in out["content"]]
    assert kinds[0] == "image" and kinds[-1] == "text"
    call = cf.calls[0]
    assert call["html"].startswith("<html>")
    assert call["viewport"]["width"] == 390
    assert call["screenshot_options"] == {"fullPage": True}


async def test_a_svelte_draft_uses_the_cached_preview_render(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "engine": "svelte", "source": {}}),
        ),
        patch("pocketpaw_ee.sites.draft_markup.build_draft_markup", new=AsyncMock(return_value="")),
        patch(
            "pocketpaw_ee.sites.service.get_native_artifact",
            new=AsyncMock(return_value={"body_html": "<main>x</main>", "css": "main{}"}),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", return_value=cf),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is not True, out
    html = cf.calls[0]["html"]
    assert "<main>x</main>" in html and "<style>main{}</style>" in html
    assert cf.calls[0]["viewport"]["width"] == 1280


async def test_a_render_still_building_says_verify_first(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "engine": "react", "source": {}}),
        ),
        patch("pocketpaw_ee.sites.draft_markup.build_draft_markup", new=AsyncMock(return_value="")),
        patch(
            "pocketpaw_ee.sites.service.get_native_artifact",
            new=AsyncMock(return_value={"body_html": "", "build_status": "queued"}),
        ),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is True
    assert "verify_site" in out["content"][0]["text"]


async def test_no_browser_rendering_is_an_honest_error(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
    from pocketpaw_ee.cloud._core.errors import ValidationError

    def _unconfigured():
        raise ValidationError("sites.cloudflare_unconfigured", "Cloudflare is not configured")

    with (
        patch(
            "pocketpaw_ee.sites.draft_markup.build_draft_markup",
            new=AsyncMock(return_value="<html></html>"),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", side_effect=_unconfigured),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is True
    assert "unavailable on this deployment" in out["content"][0]["text"]


async def test_it_needs_a_pocket_id(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    out = await mcp._preview_site_handler({})
    assert out.get("is_error") is True


def test_it_is_registered_on_the_allow_list() -> None:
    from pocketpaw_ee.agent.mcp_servers.sites import PREVIEW_SITE_TOOL_ID, SITES_TOOL_IDS
    from pocketpaw_ee.agent.mcp_servers.sites_create import SITES_CREATE_TOOL_IDS
    from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

    assert PREVIEW_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__preview_site"
    assert PREVIEW_SITE_TOOL_ID in SITES_TOOL_IDS
    assert PREVIEW_SITE_TOOL_ID in SITES_CREATE_TOOL_IDS
    allow = resolve_profile(SurfaceKind.SITES, SurfaceMeta()).allow_mcp_tool_ids
    assert allow is not None and PREVIEW_SITE_TOOL_ID in allow
