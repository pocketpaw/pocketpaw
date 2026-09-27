# tests/test_reference_images.py — turning screenshots into images a model can read.
#
# Created 2026-09-27 (feat/sites-visual-research).
#
# Two things matter here. The host allow-list, because the URLs come from third-
# party archives and a fetch-anything helper on the API host is an SSRF primitive.
# And the tiling, because a full-page capture handed over as one image arrives at
# the model downsampled to an unreadable sliver.

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from pocketpaw.tools.builtin import reference_images as ri

_HOSTS = ("images.example-archive.com",)


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clear_transport():
    yield
    ri._TRANSPORT = None


def test_host_allowed_needs_https_and_a_listed_host() -> None:
    assert ri.host_allowed("https://images.example-archive.com/a.jpg", _HOSTS)
    assert ri.host_allowed("https://cdn.images.example-archive.com/a.jpg", _HOSTS)
    assert not ri.host_allowed("http://images.example-archive.com/a.jpg", _HOSTS)
    assert not ri.host_allowed("https://evil.com/a.jpg", _HOSTS)
    # A suffix match on the raw string would let this through.
    assert not ri.host_allowed("https://notimages.example-archive.com.evil.com/a.jpg", _HOSTS)
    assert not ri.host_allowed("https://169.254.169.254/latest", _HOSTS)


async def test_an_unlisted_host_is_refused_before_any_request() -> None:
    def _never(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("fetched a host that is not on the list")

    ri._TRANSPORT = httpx.MockTransport(_never)
    with pytest.raises(ri.ReferenceImageError):
        await ri.fetch_image("https://evil.com/a.jpg", _HOSTS)


async def test_a_non_image_response_is_refused() -> None:
    ri._TRANSPORT = httpx.MockTransport(
        lambda _r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(ri.ReferenceImageError):
        await ri.fetch_image("https://images.example-archive.com/a.jpg", _HOSTS)


async def test_a_redirect_is_not_followed() -> None:
    """A listed host redirecting elsewhere must not become a fetch of elsewhere."""
    ri._TRANSPORT = httpx.MockTransport(
        lambda _r: httpx.Response(302, headers={"location": "https://evil.com/x.png"})
    )
    with pytest.raises(ri.ReferenceImageError):
        await ri.fetch_image("https://images.example-archive.com/a.jpg", _HOSTS)


async def test_a_listed_image_comes_back_as_bytes() -> None:
    png = _png(10, 10)
    ri._TRANSPORT = httpx.MockTransport(
        lambda _r: httpx.Response(200, content=png, headers={"content-type": "image/png"})
    )
    assert await ri.fetch_image("https://images.example-archive.com/a.png", _HOSTS) == png


def test_a_tall_page_is_cut_into_readable_tiles() -> None:
    tiles = ri.to_tiles(_png(2560, 8000), max_tiles=10)
    sizes = [Image.open(io.BytesIO(t)).size for t in tiles]
    # Scaled to 1280 wide, then 1600px (1.25x) tiles: 4000 / 1600 -> 3 tiles.
    assert len(tiles) == 3
    for w, h in sizes:
        assert max(w, h) <= 1568
        assert h / w <= 1.26


def test_tiles_stop_at_the_cap_and_keep_the_top() -> None:
    assert len(ri.to_tiles(_png(1280, 20000), max_tiles=2)) == 2


def test_a_short_image_is_one_tile() -> None:
    assert len(ri.to_tiles(_png(1280, 800))) == 1


def test_bytes_that_are_not_an_image_raise() -> None:
    with pytest.raises(ri.ReferenceImageError):
        ri.to_tiles(b"not an image")


def test_image_blocks_are_mcp_image_content() -> None:
    blocks = ri.image_blocks(ri.to_tiles(_png(100, 100)))
    assert blocks and blocks[0]["type"] == "image"
    assert blocks[0]["mimeType"] == "image/jpeg"


# ── size budget (fix: SDK 1 MB buffer) ──────────────────────────────────────
# The Claude Agent SDK reads each message from the CLI into a 1 MB buffer by
# default. A full-page screenshot of a busy page cut into six high-quality
# tiles came to well over that in one tool result, and the turn died with
# "JSON message exceeded maximum buffer size of 1048576 bytes".


def _noisy_png(width: int, height: int) -> bytes:
    """Random noise: the worst case for JPEG size, like a photo-heavy page."""
    import os

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_one_result_stays_under_the_image_budget() -> None:
    blocks = ri.image_blocks(ri.to_tiles(_noisy_png(1280, 9000), max_tiles=6))
    total = sum(len(b["data"]) for b in blocks)
    assert blocks, "a budget must never cost the whole picture"
    assert total <= ri.MAX_RESULT_BYTES, total
    assert ri.MAX_RESULT_BYTES <= 800_000  # far below the SDK's 1 MB message cap
