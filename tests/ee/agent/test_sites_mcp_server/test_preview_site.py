# tests/ee/agent/test_sites_mcp_server/test_preview_site.py — the agent looks at its
# own draft.
#
# ``preview_site`` photographs the current draft with Browser Rendering and returns
# image blocks. html, svelte and react drafts are rendered from markup; a
# template-built (project) draft is photographed at its preview URL, or at the live
# URL once published. These tests pin its answers: pictures, "not ready yet, build or
# verify first", "server-only, nothing to photograph", and "no screenshots on this
# deployment"; the `path` and `wait_ms` arguments; and that no answer stores
# anything or claims the site is broken.
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


async def test_a_long_page_comes_back_as_at_most_three_tiles_after_load(ctx) -> None:
    """Every tile is re-read on each later call in the turn, so the look is capped at
    the fold and the first sections, and the capture waits for ``load`` rather than
    for the network to go idle."""
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF(_png(1280, 12000))
    with (
        patch(
            "pocketpaw_ee.sites.draft_markup.build_draft_markup",
            new=AsyncMock(return_value="<html><body>hi</body></html>"),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", return_value=cf),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    images = [part for part in out["content"] if part["type"] == "image"]
    assert 1 <= len(images) <= 3
    assert cf.calls[0]["goto_options"]["waitUntil"] == "load"


async def test_it_carries_the_previous_edits_verdict(ctx) -> None:
    """A finished background build is reported on the next sites tool result, and
    preview_site (the call that usually follows an edit) is one."""
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with (
        patch(
            "pocketpaw_ee.sites.draft_markup.build_draft_markup",
            new=AsyncMock(return_value="<html><body>hi</body></html>"),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", return_value=cf),
        patch(
            "pocketpaw_ee.sites.verify.settled_verdict",
            return_value={"status": "failed", "build": "failed"},
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(
                return_value={"id": "p1", "engine": "html", "source": {}, "workspace": "w"}
            ),
        ),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out["content"][-1]["type"] == "text"
    assert "previous_verification" in out["content"][-1]["text"]


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


# A template-built (project) draft is a real page on the draft preview origin, so the
# picture is taken from its preview_url with its own scripts running. Every prod call
# on a project site answered "no rendered draft to photograph yet" before this.
async def test_a_project_draft_is_photographed_at_its_preview_url(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "engine": "project", "source": {}}),
        ),
        patch("pocketpaw_ee.sites.draft_markup.build_draft_markup", new=AsyncMock(return_value="")),
        patch(
            "pocketpaw_ee.sites.service.get_native_artifact",
            new=AsyncMock(
                return_value={
                    "body_html": "",
                    "build_status": "none",
                    "preview_url": "https://abc123.preview.example.com/",
                    "preview_mode": "static",
                }
            ),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", return_value=cf),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is not True, out
    assert out["content"][0]["type"] == "image"
    call = cf.calls[0]
    assert call["url"] == "https://abc123.preview.example.com/"
    assert "html" not in call or not call["html"]


async def test_a_project_draft_still_building_says_build_first(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "engine": "project", "source": {}}),
        ),
        patch("pocketpaw_ee.sites.draft_markup.build_draft_markup", new=AsyncMock(return_value="")),
        patch(
            "pocketpaw_ee.sites.service.get_native_artifact",
            new=AsyncMock(
                return_value={"body_html": "", "build_status": "queued", "preview_url": None}
            ),
        ),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is True
    assert "run_site_build" in out["content"][0]["text"]


def _project(artifact: dict[str, Any], cf: _FakeCF | None = None):
    """Patches for a project pocket whose draft answers ``artifact``."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value={"id": "p1", "engine": "project", "source": {}}),
        )
    )
    stack.enter_context(
        patch(
            "pocketpaw_ee.sites.service.get_native_artifact",
            new=AsyncMock(return_value={"body_html": "", **artifact}),
        )
    )
    if cf is not None:
        stack.enter_context(patch("pocketpaw_ee.sites.service._cf_client", return_value=cf))
    return stack


_BUILT = {
    "build_status": "none",
    "preview_url": "https://abc123.preview.example.com/",
    "preview_mode": "full",
}


async def test_a_project_capture_waits_for_the_page_to_settle(ctx) -> None:
    """A template-built app draws after load (a game, a three.js scene), so a url
    capture pauses before the shutter; an html capture does not."""
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with _project(_BUILT, cf):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is not True, out
    assert cf.calls[0]["wait_for_timeout"] == mcp._PREVIEW_URL_SETTLE_MS > 0
    assert cf.calls[0]["goto_options"]["waitUntil"] == "load"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/play", "https://abc123.preview.example.com/play"),
        ("/about/team", "https://abc123.preview.example.com/about/team"),
        ("/search?q=tea", "https://abc123.preview.example.com/search?q=tea"),
        ("/", "https://abc123.preview.example.com/"),
    ],
)
async def test_a_path_is_photographed_on_the_draft_origin(ctx, path, expected) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with _project(_BUILT, cf):
        out = await mcp._preview_site_handler({"pocket_id": "p1", "path": path})

    assert out.get("is_error") is not True, out
    assert cf.calls[0]["url"] == expected


@pytest.mark.parametrize(
    "path",
    [
        "play",
        "//evil.example.com/x",
        "https://evil.example.com/",
        "/x://y",
        "/../secret",
        "/a/../../b",
        "/a\\b",
        "/a b",
        "/a#frag",
        42,
    ],
)
async def test_a_path_off_the_draft_origin_is_refused(ctx, path) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with _project(_BUILT, cf):
        out = await mcp._preview_site_handler({"pocket_id": "p1", "path": path})

    assert out.get("is_error") is True
    assert "`path`" in out["content"][0]["text"]
    assert cf.calls == []


@pytest.mark.parametrize(("wait_ms", "expected"), [(0, 0), (3000, 3000), (-5, 0), (60_000, 8000)])
async def test_wait_ms_is_clamped(ctx, wait_ms, expected) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with _project(_BUILT, cf):
        out = await mcp._preview_site_handler({"pocket_id": "p1", "wait_ms": wait_ms})

    assert out.get("is_error") is not True, out
    assert cf.calls[0]["wait_for_timeout"] == expected


async def test_wait_ms_must_be_a_number(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    out = await mcp._preview_site_handler({"pocket_id": "p1", "wait_ms": "slow"})
    assert out.get("is_error") is True
    assert "`wait_ms`" in out["content"][0]["text"]


async def test_a_path_on_a_one_page_draft_is_ignored_with_a_note(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    with (
        patch(
            "pocketpaw_ee.sites.draft_markup.build_draft_markup",
            new=AsyncMock(return_value="<html><body>hi</body></html>"),
        ),
        patch("pocketpaw_ee.sites.service._cf_client", return_value=cf),
    ):
        out = await mcp._preview_site_handler({"pocket_id": "p1", "path": "/about"})

    assert out.get("is_error") is not True, out
    assert "url" not in cf.calls[0] and cf.calls[0]["wait_for_timeout"] == 0
    assert "was ignored" in out["content"][-1]["text"]


async def test_a_server_only_draft_says_there_is_nothing_to_photograph(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    with _project({"build_status": "none", "preview_url": None, "preview_mode": "server_only"}):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "no static draft page" in text and "before publish" in text
    assert "Do not describe the page as visually checked" in text
    assert "not mean the site is broken" in text


async def test_a_failed_project_build_says_fix_and_rebuild(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    with _project({"build_status": "failed", "preview_url": None, "preview_mode": None}):
        out = await mcp._preview_site_handler({"pocket_id": "p1"})

    assert out.get("is_error") is True
    assert "run_site_build" in out["content"][0]["text"]


async def test_a_published_project_is_photographed_at_its_live_url(ctx) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    cf = _FakeCF()
    live = {
        "build_status": "none",
        "preview_url": "https://bakery.paw.site",
        "preview_mode": "published",
    }
    with _project(live, cf):
        out = await mcp._preview_site_handler({"pocket_id": "p1", "path": "/menu"})

    assert out.get("is_error") is not True, out
    assert cf.calls[0]["url"] == "https://bakery.paw.site/menu"


def test_the_tool_schema_offers_path_and_wait_ms() -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    captured: dict[str, Any] = {}

    def _tool(name, description, schema):
        captured.update(name=name, description=description, schema=schema)
        return lambda fn: fn

    mcp.make_preview_site_tool(_tool)
    props = captured["schema"]["properties"]
    assert props["path"]["type"] == "string"
    assert props["wait_ms"]["type"] == "integer"
    assert "template-built" in captured["description"]
    assert "each milestone" in captured["description"]
