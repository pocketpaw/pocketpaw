# tests/ee/sites/test_capture_readiness.py — a preview is never a photograph of a
# page that was not serving yet.
#
# Created 2026-08-08. THE BUG THIS REPRODUCES: a publish returns, the site's worker
# is uploaded, and the capture fires immediately — but Cloudflare takes time to go
# live after a deploy, so the address the browser is pointed at is still answering
# 404 (or an edge placeholder). Browser Rendering renders that page happily, the
# result is a perfectly valid PNG, every fail-closed check downstream passes, and
# the bytes land on the Site as ``preview_image_url``. The card then shows a picture
# of nothing, permanently: nothing re-captures on its own, so the only escape was to
# republish an unchanged site.
#
# The failure is invisible to every guard SC-1 shipped, which is the point. Those
# guards check that a capture SUCCEEDED — 2xx, an ``image/*`` content type, non-empty
# bytes. A screenshot of a 404 satisfies all three. Nothing anywhere asked whether
# the page was worth photographing.
#
# What is pinned here:
#   * the reproduction — a publish whose url is not serving yet must end with NO
#     stored preview, and must not spend a paid Browser Rendering call on a dead
#     page;
#   * the poll — an edge that comes up on the third probe is captured, not
#     abandoned, and each probe addresses a cache-busted url so a stale 200 from the
#     edge cannot pass the gate;
#   * the probe's own semantics, over a real httpx transport — 2xx is ready,
#     everything else (404, 5xx, a connection that never opens) is not;
#   * the rule the module was founded on, unbroken — the gate lives in the
#     background capture, never on the publish's stack, and a probe that explodes
#     still cannot fail a publish;
#   * the manual half — ``refresh_site_preview`` runs a SHORT budget (a person is
#     watching a spinner) and reports a page that is not serving as its own error,
#     distinct from "there is nothing to photograph yet";
#   * the draft path is untouched — markup rendered at about:blank has no address
#     that could be unready.
#
# The mutations that break these tests (run via scripts/mutate.py — a gate nobody has
# watched fail is not a gate) live in tests/mutations/site_screenshot.json: removing
# the readiness gate from ``take_site_screenshot``; making ``_url_is_serving`` report
# any response as ready; and dropping the manual path's ``preview_not_serving``
# branch.
from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import screenshot as screenshot_mod
from pocketpaw_ee.sites import service as sites_service

# A real 1x1 PNG — the upload pipeline sniffs magic bytes, so a fake payload would
# be rejected as an unsupported mime and a happy-path test would pass for the wrong
# reason. Same constant, same reason, as test_screenshot_capture.py.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _FakeCFScreenshot:
    """A Cloudflare client that only knows how to take a screenshot — and takes one
    of WHATEVER it is pointed at. That is the real Browser Rendering behaviour and
    the whole reason this bug exists: it does not care that the page is a 404."""

    def __init__(self, result: bytes | Exception = _PNG) -> None:
        self.result = result
        self.calls: list[str] = []

    async def capture_screenshot(self, *, url="", html="", **_kw):
        self.calls.append(url or "<html>")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Edge:
    """A stand-in for the deployed page's readiness, answering a scripted sequence.

    ``ready_after=2`` means the first two probes see an edge that is not serving yet
    and the third sees the site live — the actual shape of a Cloudflare deploy."""

    def __init__(self, *, ready_after: int = 0) -> None:
        self.ready_after = ready_after
        self.probes: list[str] = []

    async def __call__(self, url: str, **_kw) -> bool:
        self.probes.append(url)
        return len(self.probes) > self.ready_after


def _site(**over):
    """An in-memory stand-in for a deployed Site doc, recording its own ``set``."""
    sets: list[dict] = []

    class _S:
        id = over.get("id", "site-1")
        owner = "u1"
        workspace = "ws1"
        pocket_id = "pocket-1"
        url = over.get("url", "https://brew.example.test/")
        preview_image_url = over.get("preview_image_url", None)
        writes = sets

        async def set(self, updates):
            sets.append(updates)

    return _S()


def _gate(monkeypatch, edge: _Edge) -> _Edge:
    """Point the readiness gate at a scripted edge and take the waits out.

    ``raising=False`` on purpose: this test file was written BEFORE the gate existed,
    against a build where these attributes are absent and the capture fires blind.
    That is the reproduction — the seam is installed, ignored, and a picture of a
    dead page is stored anyway."""
    monkeypatch.setattr(screenshot_mod, "_url_is_serving", edge, raising=False)
    monkeypatch.setattr(screenshot_mod, "_READY_DELAYS", (0, 0, 0, 0, 0), raising=False)
    return edge


# --------------------------------------------------------------------------- #
# THE REPRODUCTION
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_page_that_is_not_serving_yet_is_never_photographed(monkeypatch, tmp_path):
    """THE BUG, at the capture itself. Cloudflare has not finished going live, so the
    site's address answers 404. Browser Rendering would render that 404 into a valid
    PNG and the Site would remember it forever.

    Mutation: delete the readiness gate in ``take_site_screenshot`` and this stores a
    preview of a page that was never serving."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cf = _FakeCFScreenshot()
    edge = _gate(monkeypatch, _Edge(ready_after=99))  # never comes up
    site = _site()

    out = await screenshot_mod.take_site_screenshot(site, cloudflare=cf)

    # Nothing recorded: a card with no image is honest and the gallery already has a
    # placeholder for it. A screenshot of a 404 is a lie that outlives the deploy.
    assert site.writes == []
    assert out == ""
    # And no paid render was spent on a page that had nothing to show.
    assert cf.calls == []
    assert edge.probes, "the readiness gate never ran"


@pytest.mark.asyncio
async def test_a_publish_whose_edge_is_not_live_yet_stores_no_preview(
    beanie_test_db, monkeypatch, tmp_path, published_url
):
    """THE BUG, end to end from a real publish — the captain's report exactly. The
    publish succeeds, the worker is uploaded, and the capture fires while Cloudflare
    is still bringing the address up.

    The site must still deploy (a screenshot may never cost anybody a publish) and
    the card must be left with no image rather than a picture of nothing."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cf = _FakeCFScreenshot()
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    _gate(monkeypatch, _Edge(ready_after=99))
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    for task in ran:
        await task

    rows = await sites_service.list_for_workspace("ws1")
    assert rows[0].preview_image_url is None
    assert cf.calls == []
    # THE RULE, unbroken: the site is live regardless of what the picture did.
    assert site.deployed is True


# --------------------------------------------------------------------------- #
# The poll — an edge that comes up late is still captured
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_edge_that_comes_up_on_the_third_probe_is_captured(monkeypatch, tmp_path):
    """The gate is a poll, not a single look. Cloudflare going live takes seconds, so
    a capture that gave up on the first 404 would be the same bug with extra steps —
    it would just fail to a missing preview instead of a wrong one."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cf = _FakeCFScreenshot()
    edge = _gate(monkeypatch, _Edge(ready_after=2))
    site = _site()

    out = await screenshot_mod.take_site_screenshot(site, cloudflare=cf)

    assert len(edge.probes) == 3
    assert out.startswith("/api/v1/uploads/")
    assert site.writes == [{"preview_image_url": out}]
    assert len(cf.calls) == 1


@pytest.mark.asyncio
async def test_every_probe_addresses_a_url_the_edge_cache_has_never_seen(monkeypatch, tmp_path):
    """The probe carries SC-3's per-capture cache-buster, and a fresh one each time.

    Without it the readiness check has the same defect the screenshot had before
    SC-3: Cloudflare's edge caches by full URL, so a probe could be answered 200 from
    the document that was there BEFORE the deploy — the gate would open on the
    strength of the old page and the capture would photograph whatever came next."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    edge = _gate(monkeypatch, _Edge(ready_after=3))

    await screenshot_mod.take_site_screenshot(_site(), cloudflare=_FakeCFScreenshot())

    assert len(edge.probes) == 4
    for probe in edge.probes:
        assert probe.startswith("https://brew.example.test/?_paw_shot=")
    assert len(set(edge.probes)) == len(edge.probes), "a probe reused a cached address"


@pytest.mark.asyncio
async def test_a_site_with_no_url_is_still_skipped_before_any_probe(monkeypatch):
    """A Workers-for-Platforms deploy with no sites domain has no address at all.
    That was already skipped and stays skipped — there is nothing to poll, and
    polling "" would be a request to our own host."""
    edge = _gate(monkeypatch, _Edge())
    cf = _FakeCFScreenshot()
    site = _site(url="")

    assert await screenshot_mod.take_site_screenshot(site, cloudflare=cf) == ""
    assert edge.probes == []
    assert cf.calls == []


# --------------------------------------------------------------------------- #
# The probe itself, over a real transport
# --------------------------------------------------------------------------- #


def _probe_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# The REAL probe, bound at import — before the autouse ``_site_pages_are_serving``
# fixture in conftest.py replaces the module attribute with a stub that answers True
# without opening a connection. These four tests are the only ones in the suite about
# the probe's own semantics, so reaching it through ``screenshot_mod._url_is_serving``
# would run them against the stub and they would all pass while proving nothing.
#
# They are self-checking about it: the stub never touches the transport, so every
# ``is False`` case and the redirect's hop count fail loudly if this binding is ever
# replaced by the module attribute.
_probe = screenshot_mod._url_is_serving


@pytest.mark.asyncio
async def test_a_serving_page_reads_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<h1>Brew and Co</h1>")

    assert await _probe("https://brew.example.test/", transport=_probe_transport(handler)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 403, 500, 502, 530])
async def test_an_edge_that_is_not_serving_the_site_reads_not_ready(status):
    """404 is the ordinary pre-live answer; 5xx and Cloudflare's own 530 are the
    others. None of them is a page worth a paid render."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, html="<h1>Not found</h1>")

    assert await _probe("https://brew.example.test/", transport=_probe_transport(handler)) is False


@pytest.mark.asyncio
async def test_a_connection_that_never_opens_reads_not_ready():
    """DNS for a brand-new subdomain may not resolve yet. A transport failure is the
    earliest form of "not serving", not an error to propagate."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name or service not known")

    assert await _probe("https://brew.example.test/", transport=_probe_transport(handler)) is False


@pytest.mark.asyncio
async def test_a_redirect_to_the_live_page_reads_ready():
    """A site that redirects (http→https, bare→www) is serving. Following the hop is
    what the browser about to photograph it will do."""
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if len(hops) == 1:
            return httpx.Response(301, headers={"location": "https://brew.example.test/home"})
        return httpx.Response(200, html="<h1>Brew and Co</h1>")

    assert await _probe("https://brew.example.test/", transport=_probe_transport(handler)) is True
    assert len(hops) == 2


# --------------------------------------------------------------------------- #
# THE FOUNDING RULE — the gate may not cost anybody a publish
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_probe_that_explodes_cannot_fail_a_publish(
    beanie_test_db, monkeypatch, published_url
):
    """The readiness poll runs inside the fire-and-forget capture, so even a probe
    that raises outright lands in the same swallow every other capture failure does.

    Run INLINE, so the exception is on the publish's own stack — the strictest form
    of the rule this module was founded on."""

    async def _boom(url, **_kw):
        raise RuntimeError("dns resolver is down")

    monkeypatch.setattr(screenshot_mod, "_url_is_serving", _boom, raising=False)
    monkeypatch.setattr(screenshot_mod, "_READY_DELAYS", (), raising=False)
    cf = _FakeCFScreenshot()
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    for task in ran:
        await task

    assert site.deployed is True
    rows = await sites_service.list_for_workspace("ws1")
    assert rows[0].preview_image_url is None


@pytest.mark.asyncio
async def test_the_gate_never_runs_on_the_publishs_own_stack(
    beanie_test_db, monkeypatch, published_url, captured_screenshots
):
    """A poll that waits up to a minute would be catastrophic in ``publish()``. It is
    only ever reached through the background scheduler, so a publish with the
    scheduler stubbed out never probes at all."""
    edge = _gate(monkeypatch, _Edge(ready_after=99))

    await _publish()

    assert len(captured_screenshots) == 1
    assert edge.probes == []


@pytest.mark.asyncio
async def test_a_previous_good_preview_survives_an_unready_recapture(monkeypatch, tmp_path):
    """SC-3 re-shoots on every republish, which is right — but a republish whose edge
    has not come up yet must leave the existing picture alone rather than regress it
    to a photograph of a 404."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _gate(monkeypatch, _Edge(ready_after=99))
    site = _site(preview_image_url="/api/v1/uploads/the-good-one")

    assert await screenshot_mod.take_site_screenshot(site, cloudflare=_FakeCFScreenshot()) == ""
    assert site.writes == []
    assert site.preview_image_url == "/api/v1/uploads/the-good-one"


# --------------------------------------------------------------------------- #
# The DRAFT path — no address, so nothing to be unready
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_draft_capture_never_probes(monkeypatch, tmp_path):
    """A draft is photographed from its own markup, which renders at about:blank.
    There is no address involved, so a readiness gate there would poll nothing and
    reject every draft."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    edge = _gate(monkeypatch, _Edge(ready_after=99))
    cf = _FakeCFScreenshot()

    async def _markup(_site):
        return "<!doctype html><h1>draft</h1>"

    from pocketpaw_ee.sites import draft_markup

    monkeypatch.setattr(draft_markup, "build_draft_markup", _markup)
    site = _site(url="")

    out = await screenshot_mod.take_draft_screenshot(site, cloudflare=cf)

    assert edge.probes == []
    assert out.startswith("/api/v1/uploads/")
    assert cf.calls == ["<html>"]


# --------------------------------------------------------------------------- #
# The MANUAL half — a person is watching a spinner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_manual_refresh_of_a_page_that_is_not_serving_says_so(
    beanie_test_db, monkeypatch, tmp_path
):
    """The deploy path waits a minute because nobody is watching. This one is a
    request whose whole purpose is the answer, so it probes briefly and reports the
    real reason.

    The reason has to be its OWN error: ``preview_unavailable`` tells the operator to
    "publish the site, or open its preview once", which is exactly the wrong advice
    for a site that IS published and is merely still coming up.

    Mutation: drop the ``preview_not_serving`` branch and this fails with the
    misleading ``preview_unavailable`` instead."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sites_service, "_cf_client", lambda: _FakeCFScreenshot())
    edge = _gate(monkeypatch, _Edge(ready_after=99))
    doc = await _publish_doc(monkeypatch)

    with pytest.raises(ValidationError) as exc:
        await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(doc.id))

    assert exc.value.code == "sites.preview_not_serving"
    assert edge.probes, "the manual refresh never checked whether the page was up"


@pytest.mark.asyncio
async def test_a_manual_refresh_does_not_wait_the_full_deploy_budget(
    beanie_test_db, monkeypatch, tmp_path
):
    """A person pressing a button must not be held for the post-deploy budget. The
    manual schedule is its own, short one."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sites_service, "_cf_client", lambda: _FakeCFScreenshot())
    edge = _Edge(ready_after=99)
    monkeypatch.setattr(screenshot_mod, "_url_is_serving", edge, raising=False)
    # Deliberately NOT zeroing _READY_DELAYS: if the manual path used the deploy
    # schedule this test would sit through it.
    doc = await _publish_doc(monkeypatch)

    with pytest.raises(ValidationError):
        await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(doc.id))

    assert len(edge.probes) <= 3, f"the manual refresh probed {len(edge.probes)} times"


@pytest.mark.asyncio
async def test_a_manual_refresh_of_a_serving_page_still_captures(
    beanie_test_db, monkeypatch, tmp_path
):
    """The gate must not break the affordance it exists to make useful."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cf = _FakeCFScreenshot()
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    _gate(monkeypatch, _Edge(ready_after=0))
    doc = await _publish_doc(monkeypatch)

    out = await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(doc.id))

    assert out.preview_image_url.startswith("/api/v1/uploads/")
    assert len(cf.calls) == 1


# --------------------------------------------------------------------------- #
# Publish plumbing — shared with test_screenshot_capture.py
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


async def _publish_doc(monkeypatch):
    """Publish a live site WITHOUT letting the automatic capture run, and hand back
    the persisted doc — the starting state a manual refresh acts on."""
    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "paw-sites.test")
    monkeypatch.setattr(sites_service, "_schedule_site_screenshot", lambda site: None)
    return await _publish()


@pytest.fixture
def captured_screenshots(monkeypatch):
    """Record the sites a publish schedules a screenshot for, without taking one."""
    seen: list = []
    monkeypatch.setattr(sites_service, "_schedule_site_screenshot", seen.append)
    return seen


@pytest.fixture
def published_url(monkeypatch):
    """Give the Workers-for-Platforms publish a public address. Without
    PAW_CF_SITES_DOMAIN a WfP deploy stamps ``url=""`` and the capture correctly
    skips a site with no page to photograph — a test that wants to prove the GATE
    ran has to configure the domain, or it passes for the wrong reason."""
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
