# tests/ee/agent/test_sites_mcp_server/test_compare_site_to_reference.py — the agent
# stores an owner-supplied design and compares its draft against it.
#
# Rendering is mocked (a fake Cloudflare client) and storage is in memory. Pins:
# save reads this workspace's public assets from storage and fetches only from the
# research image hosts, refusing every other host before any request; a foreign or
# other-workspace pocket is refused; compare renders the draft at the reference's
# width through the same capture path as preview_site and returns side-by-sides,
# a heatmap and a summary with regions and tile scores; failures say no comparison
# ran; and both tools ride the /sites allow-list.
from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from PIL import Image, ImageDraw  # noqa: E402


def _page(width: int = 1280, height: int = 3000, *, block: bool = False) -> bytes:
    image = Image.new("RGB", (width, height), (245, 245, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, height // 10), fill=(30, 30, 40))
    if block:
        draw.rectangle((100, 500, 500, 900), fill=(200, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _MemAdapter:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> None:
        self.files[key] = b"".join([chunk async for chunk in stream])

    async def open(self, key: str) -> AsyncIterator[bytes]:
        from pocketpaw.uploads.errors import NotFound

        if key not in self.files:
            raise NotFound(key)
        yield self.files[key]

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.files if k.startswith(prefix)]


class _FakeCF:
    def __init__(self, image: bytes) -> None:
        self.image = image
        self.calls: list[dict[str, Any]] = []

    async def capture_screenshot(self, **kwargs: Any) -> bytes:
        self.calls.append(kwargs)
        return self.image


class _FakeStore:
    """The public asset rail: maps only this workspace's own URLs back to keys."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read_keys: list[str] = []

    def key_for_url(self, url: str, *, workspace_id: str, pocket_id: str | None = None):
        prefix = f"https://assets.example.test/sites-assets/{workspace_id}/"
        return url.removeprefix("https://assets.example.test/") if url.startswith(prefix) else None

    async def read(self, key: str, *, max_bytes: int) -> bytes:
        self.read_keys.append(key)
        return self.data


@pytest.fixture
def adapter():
    mem = _MemAdapter()
    with patch("pocketpaw_ee.sites.site_references._adapter", return_value=mem):
        yield mem


@pytest.fixture
def ctx(adapter):
    with (
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
        patch(
            "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
            new=AsyncMock(return_value="go"),
        ),
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(
                return_value={"id": "p1", "engine": "html", "source": {}, "workspace": "w"}
            ),
        ),
        patch(
            "pocketpaw_ee.sites.draft_markup.build_draft_markup",
            new=AsyncMock(return_value="<html><body>hi</body></html>"),
        ),
    ):
        yield


async def _save(data: bytes, url: str = "https://assets.example.test/sites-assets/w/p1/a-ref.png"):
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp

    with patch(
        "pocketpaw_ee.sites.public_assets.public_asset_store", return_value=_FakeStore(data)
    ):
        return await mcp._save_site_reference_handler({"pocket_id": "p1", "image_url": url})


async def test_save_reads_the_owners_attachment_from_storage(ctx, adapter) -> None:
    out = await _save(_page())
    assert out.get("is_error") is not True, out
    body = json.loads(out["content"][0]["text"])
    assert body["reference_id"].startswith("ref_")
    assert (body["width"], body["height"], body["looks_like"]) == (1280, 3000, "desktop")
    assert list(adapter.files) == [f"sites-references/w/p1/{body['reference_id']}.png"]


async def test_save_refuses_other_hosts_before_any_request(ctx, adapter) -> None:
    from pocketpaw.tools.builtin import reference_images

    fetch = AsyncMock()
    with patch.object(reference_images, "fetch_image", fetch):
        for url in (
            "https://evil.example.com/a.png",
            "http://images.refero.design/a.png",
            "https://169.254.169.254/latest/meta-data",
            "https://assets.example.test/sites-assets/other-ws/p1/a.png",
        ):
            out = await _save(_page(), url)
            assert out.get("is_error") is True, url
            assert "Nothing was saved" in out["content"][0]["text"]
    fetch.assert_not_called()
    assert adapter.files == {}


async def test_save_fetches_from_a_research_host(ctx, adapter) -> None:
    from pocketpaw.tools.builtin import reference_images

    fetch = AsyncMock(return_value=_page())
    with patch.object(reference_images, "fetch_image", fetch):
        out = await _save(_page(), "https://images.refero.design/screens/1.png")
    assert out.get("is_error") is not True, out
    fetch.assert_awaited_once()


async def test_save_refuses_a_non_image(ctx, adapter) -> None:
    out = await _save(b"<svg onload='x'></svg>")
    assert out.get("is_error") is True
    assert adapter.files == {}


async def test_a_pocket_in_another_workspace_is_refused(ctx, adapter) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value={"id": "p1", "workspace": "other"}),
    ):
        saved = await _save(_page())
        compared = await mcp._compare_site_to_reference_handler(
            {"pocket_id": "p1", "reference_id": "ref_0123456789abcdef"}
        )
    assert saved.get("is_error") is True
    assert compared.get("is_error") is True
    assert adapter.files == {}


async def test_compare_renders_at_the_reference_width_and_reports(ctx, adapter) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp

    ref_id = json.loads((await _save(_page()))["content"][0]["text"])["reference_id"]
    cf = _FakeCF(_page(block=True))
    with patch("pocketpaw_ee.sites.service._cf_client", return_value=cf):
        out = await mcp._compare_site_to_reference_handler(
            {"pocket_id": "p1", "reference_id": ref_id, "viewport": 1440}
        )
    assert out.get("is_error") is not True, out
    call = cf.calls[0]
    assert call["viewport"] == {"width": 1440, "height": 800}
    assert call["screenshot_options"] == {"fullPage": True}
    assert call["html"].startswith("<html>")
    kinds = [part["type"] for part in out["content"]]
    assert kinds[-1] == "text" and kinds.count("image") >= 2
    text = out["content"][-1]["text"]
    assert "at 1440px" in text
    assert "Score per tile" in text
    assert "Biggest mismatches" in text
    assert "at least two passes" in text


async def test_compare_defaults_to_the_guessed_width(ctx, adapter) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp

    ref_id = json.loads((await _save(_page(1170, 2532)))["content"][0]["text"])["reference_id"]
    cf = _FakeCF(_page(390, 1800))
    with patch("pocketpaw_ee.sites.service._cf_client", return_value=cf):
        out = await mcp._compare_site_to_reference_handler(
            {"pocket_id": "p1", "reference_id": ref_id}
        )
    assert out.get("is_error") is not True, out
    assert cf.calls[0]["viewport"]["width"] == 390
    assert "(mobile)" in out["content"][-1]["text"]


async def test_compare_with_an_unknown_reference_says_save_first(ctx, adapter) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp

    out = await mcp._compare_site_to_reference_handler(
        {"pocket_id": "p1", "reference_id": "ref_0123456789abcdef"}
    )
    assert out.get("is_error") is True
    assert "save_site_reference" in out["content"][0]["text"]


async def test_no_screenshots_means_no_comparison_not_a_broken_site(ctx, adapter) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp
    from pocketpaw_ee.cloud._core.errors import CloudError

    ref_id = json.loads((await _save(_page()))["content"][0]["text"])["reference_id"]

    class _NoCF:
        async def capture_screenshot(self, **_: Any) -> bytes:
            raise CloudError(503, "cf.unconfigured", "not configured")

    with patch("pocketpaw_ee.sites.service._cf_client", return_value=_NoCF()):
        out = await mcp._compare_site_to_reference_handler(
            {"pocket_id": "p1", "reference_id": ref_id}
        )
    assert out.get("is_error") is True
    assert "unavailable" in out["content"][0]["text"]


async def test_bad_viewport_is_refused_before_rendering(ctx, adapter) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_reference as mcp

    ref_id = json.loads((await _save(_page()))["content"][0]["text"])["reference_id"]
    cf = _FakeCF(_page())
    with patch("pocketpaw_ee.sites.service._cf_client", return_value=cf):
        out = await mcp._compare_site_to_reference_handler(
            {"pocket_id": "p1", "reference_id": ref_id, "viewport": 5000}
        )
    assert out.get("is_error") is True
    assert cf.calls == []


def test_both_tools_ride_the_sites_allow_list() -> None:
    from pocketpaw_ee.agent.mcp_servers.sites import (
        COMPARE_SITE_TO_REFERENCE_TOOL_ID,
        SAVE_SITE_REFERENCE_TOOL_ID,
        SITES_TOOL_IDS,
    )
    from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

    assert COMPARE_SITE_TO_REFERENCE_TOOL_ID == (
        "mcp__pocketpaw_sites_manager__compare_site_to_reference"
    )
    for tool_id in (SAVE_SITE_REFERENCE_TOOL_ID, COMPARE_SITE_TO_REFERENCE_TOOL_ID):
        assert tool_id in SITES_TOOL_IDS
        for meta in (SurfaceMeta(), SurfaceMeta(pocket_id="p1")):
            profile = resolve_profile(SurfaceKind.SITES, meta)
            assert tool_id in (profile.allow_mcp_tool_ids or ()), (tool_id, meta)
            assert tool_id not in profile.deny_mcp_tool_ids


def test_the_server_registers_both_tools() -> None:
    from pocketpaw_ee.agent.mcp_servers.sites_reference import (
        make_compare_site_to_reference_tool,
        make_save_site_reference_tool,
    )

    names: list[str] = []

    def tool(name: str, description: str, schema: dict[str, Any]):
        names.append(name)
        assert schema["additionalProperties"] is False
        return lambda fn: fn

    make_save_site_reference_tool(tool)
    make_compare_site_to_reference_tool(tool)
    assert names == ["save_site_reference", "compare_site_to_reference"]


def test_the_reference_build_skill_names_both_tools() -> None:
    """The skill that drives the loop has to name the tools, or the agent keeps
    comparing by eye with preview_site."""
    from pocketpaw.bundled_skills.installer import bundled_skills_plugin_dir

    plugin_dir = bundled_skills_plugin_dir()
    assert plugin_dir is not None
    text = (plugin_dir / "skills" / "sites-reference-build" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "`save_site_reference`" in text
    assert "`compare_site_to_reference`" in text
