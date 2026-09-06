# tests/cloud/test_browser_mcp.py — BR-4's two additions to the /browser MCP
# server: ``extract`` (read a page as markdown) and the screenshot's saved asset
# URL.
#
# Created: 2026-09-06 (BR-4, feat/browser-surface-extract).
#
# The extract tests come in two halves on purpose:
#   * The SIZE tests drive a REAL Chromium, because the claim of this slice is
#     about the snapshot of the SAME page and a mocked page can be made to say
#     anything. They skip cleanly when no browser is installed, and they measure
#     the UNTRUNCATED extract (``max_chars`` far above the page) so a ratio is a
#     property of the conversion rather than of the cap. The measured numbers —
#     including the ones that do NOT meet the PRD's target — are recorded above
#     those tests.
#   * Everything else is handler-level with a stub driver: no browser needed.
#
# The screenshot tests prove the URL RESOLVES (a TestClient GET returns the PNG
# bytes) and that it resolves for the owning workspace ONLY — a capture carries
# its owner in the filename because ``serve_media`` refuses any name containing
# a slash, so a per-workspace subdirectory could never have a URL at all.

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.agent.mcp_servers import browser as browser_mcp
from pocketpaw_ee.cloud.media import router as media_router_module
from pocketpaw_ee.cloud.media import storage as media_storage
from pocketpaw_ee.cloud.media.router import router as media_router

from pocketpaw.browser.snapshot import MAX_SNAPSHOT_CHARS
from pocketpaw.uploads.local import LocalStorageAdapter

pytestmark = pytest.mark.asyncio

# A 1x1 transparent PNG — enough to prove bytes made the round trip.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    """The audit sink wants a runtime DB; the rows themselves are BR-1's tests."""
    monkeypatch.setattr(browser_mcp, "record_tool_call", lambda **kw: None)


@pytest.fixture
def media_store(tmp_path, monkeypatch):
    """Media adapter on a tmp dir — captures land at <root>/generated/<name>."""
    root = tmp_path / "media-root"
    (root / "generated").mkdir(parents=True)
    monkeypatch.setattr(media_storage, "_ADAPTER", LocalStorageAdapter(root=root))
    return root / "generated"


class _StubDriver:
    """Just enough driver for the handlers: HTML in, PNG out."""

    def __init__(self, html: str = "<html><body><p>hi</p></body></html>") -> None:
        self._html = html
        self.current_url = "https://example.com/page"

    async def content_html(self) -> str:
        return self._html

    async def screenshot_png(self) -> bytes:
        return _PNG


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# --- extract ------------------------------------------------------------------


async def test_extract_returns_markdown_without_link_urls_or_images():
    """MUTATION: flip ``ignore_links``/``ignore_images`` off — the href and the
    image alt come back and the output stops being cheaper than the snapshot."""
    html = (
        "<html><body><h1>Title</h1>"
        '<p>See <a href="https://example.com/a/very/long/tracking/url?utm=1">the docs</a>.</p>'
        '<img src="https://example.com/pic.png" alt="a picture">'
        "</body></html>"
    )
    with (
        patch.object(browser_mcp, "_identity", return_value=("ws-1", "u-1")),
        patch.object(browser_mcp, "_driver", return_value=_StubDriver(html)),
    ):
        body = _payload(await browser_mcp._extract_handler({}))

    assert body["ok"] is True
    assert "# Title" in body["markdown"]
    assert "the docs" in body["markdown"]
    assert "utm=1" not in body["markdown"]
    assert "a picture" not in body["markdown"]
    assert body["truncated"] is False


async def test_extract_marks_truncation():
    """A silently-cut page is worse than a short one: the agent answers
    confidently about content it never saw.

    MUTATION: drop the marker branch and just slice the text."""
    html = "<html><body><p>" + ("word " * 4000) + "</p></body></html>"
    with (
        patch.object(browser_mcp, "_identity", return_value=("ws-1", "u-1")),
        patch.object(browser_mcp, "_driver", return_value=_StubDriver(html)),
    ):
        body = _payload(await browser_mcp._extract_handler({"max_chars": 500}))

    assert body["truncated"] is True
    assert body["chars"] > 500
    assert "[truncated: 500 of" in body["markdown"]


async def test_extract_needs_a_workspace():
    with patch.object(browser_mcp, "_identity", return_value=(None, None)):
        result = await browser_mcp._extract_handler({})
    assert result["is_error"] is True


async def test_extract_tool_id_is_scoped_like_its_siblings():
    """The surface deny/allow sets are built FROM this tuple, so a tool missing
    from it is reachable on every OTHER surface. MUTATION: drop the id."""
    assert browser_mcp.EXTRACT_TOOL_ID in browser_mcp.BROWSER_TOOL_IDS
    assert browser_mcp.EXTRACT_TOOL_ID == "mcp__pocketpaw_browser__extract"


# --- the token win, against a real browser ------------------------------------
#
# MEASURED, NOT ASSUMED — and the PRD's "extract under a third of the snapshot"
# target does NOT hold. Real pages, this branch, Chromium:
#
#   en.wikipedia.org/wiki/Python_(programming_language)
#       snapshot 20,154 chars (AT ITS 20k CAP, i.e. truncated)  extract 98,961
#   news.ycombinator.com   snapshot 9,537   extract 4,076  (0.43x)
#   example.com            snapshot 191     extract 131    (0.69x)
#
# The reason is in ``snapshot.py``: every text leaf is sliced to 80 characters
# and the whole thing is capped at ``MAX_SNAPSHOT_CHARS``. So a snapshot is not
# a cheap full read — it is an INCOMPLETE one. On a link-dense page extract is
# smaller outright; on a prose page the snapshot is smaller only because it
# threw the prose away, which is precisely what an agent asked to READ the page
# cannot afford. Both halves are pinned below.

_LINK_DENSE_PAGE = (
    "<html><head><title>Feed</title></head><body><main>"
    + "".join(
        f'<div><a href="https://example.com/story/{i}?utm_source=feed&utm_campaign=x">'
        f"Story headline number {i} about something</a>"
        f"<span> 120 points by user{i} </span>"
        f'<a href="https://example.com/comments/{i}?ref=list">45 comments</a></div>'
        for i in range(120)
    )
    + "</main></body></html>"
)

_SENTENCE = "Ordinary prose a reader would want to read in full, all the way to the end of it."
_PROSE_PAGE = (
    "<html><head><title>Article</title></head><body><main><h1>Article</h1>"
    + "".join(f"<h2>Heading {i}</h2><p>{_SENTENCE * 4}</p>" for i in range(40))
    + "</main></body></html>"
)


async def _rendered(html: str):
    """(snapshot text, extracted markdown) for one page in a REAL browser.

    A mocked page can be made to say anything, and the size claim is the whole
    point of the slice — so it is measured against Chromium or skipped.
    """
    from pocketpaw.browser import BrowserDriver

    driver = BrowserDriver()

    async def _no_install():
        raise RuntimeError("browser auto-install disabled under test")

    driver._install_chromium = _no_install  # type: ignore[method-assign]
    try:
        await driver.launch()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no browser available: {exc}")

    try:
        await driver._require_page().set_content(html)
        snapshot = (await driver.snapshot()).snapshot
        with (
            patch.object(browser_mcp, "_identity", return_value=("ws-1", "u-1")),
            patch.object(browser_mcp, "_driver", return_value=driver),
        ):
            # Untruncated on purpose: a cap below the page would make the ratio
            # a property of ``max_chars`` rather than of the conversion.
            body = _payload(await browser_mcp._extract_handler({"max_chars": 200_000}))
    finally:
        await driver.close()

    assert body["truncated"] is False, "raise max_chars — measure the ratio untruncated"
    return snapshot, body["markdown"]


async def test_extract_is_smaller_than_the_snapshot_on_a_link_dense_page():
    """0.63x here, 0.43x on the live HN front page. NOT the PRD's 0.33x.

    Nav and listing pages are mostly link scaffolding by weight, which is what
    ``ignore_links`` drops. MUTATION: set ``ignore_links = False`` in
    ``_to_markdown`` — the tracking URLs come back and the ratio goes over 1.
    """
    snapshot, markdown = await _rendered(_LINK_DENSE_PAGE)

    assert "snapshot truncated" not in snapshot, "ratio would be against the cap, not the page"
    ratio = len(markdown) / len(snapshot)
    assert ratio < 0.8, f"extract {len(markdown)} vs snapshot {len(snapshot)} (ratio {ratio:.2f})"


async def test_the_snapshot_cannot_be_read_but_the_extract_can():
    """The real reason ``extract`` exists — and why the size ratio is the wrong
    measure on a prose page.

    ``snapshot.py`` slices every text leaf to 80 characters, so a paragraph
    reaches the agent as its first line and nothing else. The snapshot is
    SMALLER on this page (4.5k vs 17k) purely because it discarded the content
    the user asked about.

    MUTATION: return ``page.content()`` raw instead of converting it, or point
    ``extract`` at the snapshot — the sentence stops surviving whole.
    """
    snapshot, markdown = await _rendered(_PROSE_PAGE)

    assert markdown.count(_SENTENCE) >= 40, "extract must carry the prose whole"
    assert _SENTENCE not in snapshot, "snapshot is expected to slice text leaves at 80 chars"


async def test_the_default_read_is_bounded_below_the_snapshot_cap():
    """A default ``extract`` can never cost more than a snapshot can.

    MUTATION: raise ``DEFAULT_EXTRACT_CHARS`` above ``MAX_SNAPSHOT_CHARS``."""
    assert browser_mcp.DEFAULT_EXTRACT_CHARS < MAX_SNAPSHOT_CHARS


# --- screenshot as a renderable asset -----------------------------------------


@pytest.fixture
def media_client(media_store):
    """The media router alone, with the optional-workspace dep overridable."""
    app = FastAPI()
    app.include_router(media_router, prefix="/api/v1")

    def _as(workspace_id: str | None):
        app.dependency_overrides[media_router_module.optional_workspace_id] = lambda: workspace_id

    app.dependency_overrides[media_router_module.optional_workspace_id] = lambda: None
    client = TestClient(app)
    client.as_workspace = _as  # type: ignore[attr-defined]
    return client


async def _capture(workspace_id: str) -> dict:
    with (
        patch.object(browser_mcp, "_identity", return_value=(workspace_id, "u-1")),
        patch.object(browser_mcp, "_driver", return_value=_StubDriver()),
    ):
        return await browser_mcp._screenshot_handler({})


def _url_from(result: dict) -> str:
    note = result["content"][1]["text"]
    assert "image URL: " in note, note
    return note.split("image URL: ")[1].strip()


async def test_screenshot_returns_both_the_image_and_a_url(media_store):
    """MUTATION: drop the save and go back to bytes only — the agent has nothing
    to put in an image widget and the preamble's promise becomes a lie."""
    result = await _capture("ws-1")

    assert result["content"][0]["type"] == "image"
    url = _url_from(result)
    assert url.startswith("/api/v1/media/")

    name = url.rsplit("/", 1)[1]
    assert (media_store / name).read_bytes() == _PNG


async def test_capture_is_namespaced_to_its_workspace(media_store):
    """MUTATION: drop ``name_prefix`` — captures become indistinguishable from
    gallery files and the serve guard has nothing to check."""
    url = _url_from(await _capture("ws-1"))
    name = url.rsplit("/", 1)[1]

    assert media_storage.capture_owner_of(name) == media_storage.capture_owner_token("ws-1")
    assert media_storage.capture_owner_of(name) != media_storage.capture_owner_token("ws-2")
    assert "ws-1" not in name, "the raw tenant id must not travel in a URL"
    # A user's upload that merely starts with the prefix is NOT a capture —
    # treating it as one would hide it from the gallery and 404 it for everyone.
    assert media_storage.capture_owner_of("browser-screenshot.png") is None


async def test_the_capture_url_resolves_for_its_owner_only(media_client, media_store):
    """The URL is only a URL if it RESOLVES — and only safe if it resolves for
    one tenant. MUTATION: delete the owner check in ``serve_media``."""
    url = _url_from(await _capture("ws-1"))

    media_client.as_workspace("ws-1")
    owner = media_client.get(url)
    assert owner.status_code == 200
    assert owner.headers["content-type"].startswith("image/png")
    assert owner.content == _PNG

    media_client.as_workspace("ws-2")
    assert media_client.get(url).status_code == 404

    media_client.as_workspace(None)
    assert media_client.get(url).status_code == 404


async def test_ordinary_gallery_files_still_serve_without_a_session(media_client, media_store):
    """The guard must not turn the studio gallery into an authenticated route.

    MUTATION: apply the owner check to every name, not just captures."""
    (media_store / "1700000000000-abc123.png").write_bytes(_PNG)

    media_client.as_workspace(None)
    assert media_client.get("/api/v1/media/1700000000000-abc123.png").status_code == 200


async def test_captures_stay_out_of_the_studio_gallery(media_client, media_store):
    """A capture is not a gallery item, and listing one in every workspace would
    leak its name. MUTATION: drop the capture predicate from the listings."""
    (media_store / "1700000000000-abc123.png").write_bytes(_PNG)
    capture_url = _url_from(await _capture("ws-1"))

    with patch.object(media_router_module, "tracked_generation_filenames", return_value=set()):
        media_client.as_workspace("ws-1")
        names = [e["name"] for e in media_client.get("/api/v1/media").json()["media"]]

    assert "1700000000000-abc123.png" in names
    assert capture_url.rsplit("/", 1)[1] not in names
