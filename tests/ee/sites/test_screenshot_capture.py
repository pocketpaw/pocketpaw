# tests/ee/sites/test_screenshot_capture.py — a deployed site's card shows its own
# screenshot (SC-1), and taking that screenshot can never cost anyone a publish.
#
# Created 2026-08-07. Three things are pinned here:
#   * the Cloudflare client's Browser Rendering call — the one method whose SUCCESS
#     path is raw image bytes rather than the JSON envelope, and which must fail
#     closed on everything else so a Cloudflare error page can't be persisted as a
#     site's preview;
#   * the wiring — a LIVE publish schedules a capture, a PREVIEW publish does not,
#     and a successful capture lands on the Site and surfaces on the list DTO;
#   * THE RULE — a capture that raises, at any layer, cannot propagate into the
#     publish path. A site that is already deployed and serving must never report
#     failure because a picture of it could not be taken. Cloudflare Browser
#     Rendering is paid, quota'd and remote, so this is the expected case, not the
#     exotic one.
#
# The mutations that break these tests (run via scripts/mutate.py — a gate nobody
# has watched fail is not a gate): deleting the try/except in
# ``service._schedule_site_screenshot`` breaks the scheduler-raises test; deleting
# the try/except in ``screenshot.safe_take_site_screenshot`` breaks the
# capture-raises test; returning ``resp.content`` unconditionally in
# ``capture_screenshot`` breaks the error-envelope test.
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import screenshot as screenshot_mod
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

# A real 1x1 PNG. It has to be a real one: the upload pipeline sniffs magic bytes
# and would reject b"not-a-png" as an unsupported mime, so a fake payload would
# make the happy-path test pass for the wrong reason (or fail for a fake one).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _cf(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id="acct1",
        api_token="tok",
        zone_id="zone1",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )


# --------------------------------------------------------------------------- #
# The Cloudflare Browser Rendering call
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_capture_screenshot_returns_the_raw_image_bytes():
    """A rendered screenshot comes back as the image itself, not a JSON envelope —
    so this is the one method here that must NOT run its 2xx through ``_unwrap``."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    out = await _cf(handler).capture_screenshot(
        url="https://example.test/",
        viewport={"width": 1280, "height": 800},
        goto_options={"waitUntil": "networkidle0", "timeout": 20_000},
        screenshot_options={"fullPage": False},
    )

    assert out == _PNG
    assert seen["url"].endswith("/accounts/acct1/browser-rendering/screenshot")
    assert seen["body"]["url"] == "https://example.test/"
    assert seen["body"]["viewport"] == {"width": 1280, "height": 800}
    assert seen["body"]["gotoOptions"]["waitUntil"] == "networkidle0"
    assert seen["body"]["screenshotOptions"] == {"fullPage": False}


@pytest.mark.asyncio
async def test_capture_screenshot_never_sends_quality_with_the_default_png():
    """``quality`` is incompatible with the default .png and Cloudflare answers 400.
    The production options this service sends must not carry one — if a later change
    wants quality it has to set an explicit jpeg/webp ``type`` at the same time."""
    assert "quality" not in screenshot_mod._SCREENSHOT_OPTIONS
    assert "type" not in screenshot_mod._SCREENSHOT_OPTIONS


@pytest.mark.asyncio
async def test_capture_screenshot_fails_closed_on_a_cloudflare_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"success": False, "errors": [{"message": "bad request"}]})

    with pytest.raises(ValidationError) as exc:
        await _cf(handler).capture_screenshot(url="https://example.test/")
    assert exc.value.code == "sites.cloudflare_error"


@pytest.mark.asyncio
async def test_capture_screenshot_refuses_a_200_that_is_not_an_image():
    """Cloudflare reports some failures with a 200 + an error envelope. Returning
    that body as "image bytes" would store a JSON blob as the site's preview and
    the card would render a broken image instead of falling back to text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "errors": [{"message": "navigation timed out"}]},
        )

    with pytest.raises(ValidationError) as exc:
        await _cf(handler).capture_screenshot(url="https://example.test/")
    assert exc.value.code == "sites.cloudflare_error"


# --------------------------------------------------------------------------- #
# The capture itself
# --------------------------------------------------------------------------- #


class _FakeCFScreenshot:
    """A Cloudflare client that only knows how to take a screenshot."""

    def __init__(self, result: bytes | Exception = _PNG) -> None:
        self.result = result
        self.calls: list[str] = []

    async def capture_screenshot(self, *, url, **_kw):
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _site(**over):
    """An in-memory stand-in for a deployed Site doc, recording its own ``set``."""
    sets: list[dict] = []

    class _S:
        id = over.get("id", "site-1")
        owner = "u1"
        workspace = "ws1"
        pocket_id = "pocket-1"
        url = over.get("url", "https://brew.example.test/")
        writes = sets

        async def set(self, updates):
            sets.append(updates)

    return _S()


@pytest.mark.asyncio
async def test_a_capture_stores_the_image_and_records_it_on_the_site(monkeypatch, tmp_path):
    """The happy path end to end: shoot the live url, put the bytes in the tenant's
    blob storage, remember the resulting link on the Site."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cf = _FakeCFScreenshot()
    site = _site()

    url = await screenshot_mod.take_site_screenshot(site, cloudflare=cf)

    assert cf.calls == ["https://brew.example.test/"]
    assert url.startswith("/api/v1/uploads/")
    assert site.writes == [{"preview_image_url": url}]


@pytest.mark.asyncio
async def test_a_site_with_no_public_url_is_skipped_not_guessed_at():
    """A Workers-for-Platforms deploy with no sites domain configured has no public
    address. There is no page to photograph, and guessing one would photograph
    somebody else's."""
    cf = _FakeCFScreenshot()
    site = _site(url="")

    assert await screenshot_mod.take_site_screenshot(site, cloudflare=cf) == ""
    assert cf.calls == []
    assert site.writes == []


@pytest.mark.asyncio
async def test_a_raising_capture_is_swallowed_by_the_safe_form():
    """``safe_take_site_screenshot`` is the boundary the publish path calls through.
    Mutation: remove its try/except and this raises instead of returning ""."""
    cf = _FakeCFScreenshot(result=RuntimeError("browser rendering quota exceeded"))
    site = _site()

    assert await screenshot_mod.safe_take_site_screenshot(site, cloudflare=cf) == ""
    assert site.writes == []


# --------------------------------------------------------------------------- #
# The publish path — the rule this whole slice exists to uphold
# --------------------------------------------------------------------------- #


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True


async def _publish(*, preview: bool = False):
    return await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pocket-1",
        ripple_spec={"type": "container"},
        theme={},
        name="Brew and Co",
        preview=preview,
        _generator=_FakeGenerator(),
        _cloudflare=None if preview else _FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=(lambda site_id, project_dir: f"http://localhost/{site_id}"),
    )


@pytest.fixture
def captured_screenshots(monkeypatch):
    """Record the sites a publish schedules a screenshot for, without taking one."""
    seen: list = []
    monkeypatch.setattr(sites_service, "_schedule_site_screenshot", seen.append)
    return seen


@pytest.fixture
def published_url(monkeypatch):
    """Give the Workers-for-Platforms publish a public address.

    Without PAW_CF_SITES_DOMAIN a WfP deploy stamps ``url=""`` — the worker is
    uploaded but unreachable — and the capture correctly skips a site with no page
    to photograph. A test that wants to prove the capture RAN has to configure the
    domain, or it passes for the wrong reason."""
    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "paw-sites.test")


def _run_captures_inline(monkeypatch) -> list:
    """Make the fire-and-forget capture run on the publish's own loop and hand back
    the tasks, so a test can await the background work instead of racing it."""
    ran: list = []

    def _inline(coro):
        import asyncio

        ran.append(asyncio.get_running_loop().create_task(coro))

    monkeypatch.setattr(screenshot_mod, "_default_screenshot_scheduler", _inline)
    return ran


@pytest.mark.asyncio
async def test_a_live_publish_schedules_a_screenshot(beanie_test_db, captured_screenshots):
    """The page the card shows just changed, so the picture of it is stale."""
    await _publish()
    assert len(captured_screenshots) == 1
    assert captured_screenshots[0].pocket_id == "pocket-1"


@pytest.mark.asyncio
async def test_a_preview_publish_does_not_photograph_a_draft(
    monkeypatch, beanie_test_db, captured_screenshots
):
    """A preview is a draft nobody has approved. Its card must keep showing the
    page visitors can actually see."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    await _publish(preview=True)
    assert captured_screenshots == []


@pytest.mark.asyncio
async def test_publish_survives_a_broken_screenshot_scheduler(beanie_test_db, monkeypatch):
    """THE RULE, at the scheduling layer. The capture fires from the tail of a LIVE
    deploy, so anything escaping it would fail a publish of a site that is already
    deployed and serving.

    Mutation: delete the try/except in ``service._schedule_site_screenshot`` and
    this test fails with "scheduler down" instead of a deployed site."""

    def _boom(coro):
        coro.close()
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(screenshot_mod, "_default_screenshot_scheduler", _boom)

    site = await _publish()

    assert site.deployed is True


@pytest.mark.asyncio
async def test_publish_survives_a_cloudflare_that_refuses_to_screenshot(
    beanie_test_db, monkeypatch, published_url
):
    """THE RULE, at the Cloudflare layer, with the capture running INLINE so a
    raising Browser Rendering call is on the publish's own stack — the strictest
    form of "cannot propagate into the publish path". The site still deploys and
    the card is left with no image, which is the pre-SC-1 card.

    Mutation: delete the try/except in ``screenshot.safe_take_site_screenshot`` and
    this fails with "browser rendering is down"."""
    cf = _FakeCFScreenshot(result=RuntimeError("browser rendering is down"))
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    for task in ran:
        await task

    # The capture really was attempted and really did raise — otherwise this test
    # would pass on a site that was simply never photographed.
    assert cf.calls and cf.calls[0].endswith(".paw-sites.test")
    assert site.deployed is True

    rows = await sites_service.list_for_workspace("ws1")
    assert rows[0].preview_image_url is None


@pytest.mark.asyncio
async def test_a_captured_screenshot_reaches_the_gallery_list_item(
    beanie_test_db, monkeypatch, tmp_path, published_url
):
    """The whole point: a freshly published site's list row carries a real image
    url, which is what lets the card render the page instead of three pills."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sites_service, "_cf_client", lambda: _FakeCFScreenshot())
    ran = _run_captures_inline(monkeypatch)

    await _publish()
    for task in ran:
        await task

    rows = await sites_service.list_for_workspace("ws1")
    assert len(rows) == 1
    assert (rows[0].preview_image_url or "").startswith("/api/v1/uploads/")


@pytest.mark.asyncio
async def test_a_site_with_no_screenshot_reports_none_not_an_empty_string(
    beanie_test_db, captured_screenshots
):
    """The card's fallback branch keys on absence, so a site that has never been
    photographed must read null on the wire, not ""."""
    await _publish()

    rows = await sites_service.list_for_workspace("ws1")
    assert len(rows) == 1
    assert rows[0].preview_image_url is None
