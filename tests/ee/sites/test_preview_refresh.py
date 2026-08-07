# tests/ee/sites/test_preview_refresh.py — the card stops lying after a republish
# (SC-3), and there is a way to fix a card without republishing.
#
# Created 2026-08-07. SC-1 made a deploy take a screenshot; this file is about what
# happens the SECOND time. The claim under test is not "a capture is scheduled" —
# SC-1 pins that — but the end-to-end one a user would make: **edit a site,
# republish it, and the gallery card shows the new design.** So the republish test
# here does not assert on a scheduler or a mock call count; it publishes twice with
# two genuinely different images, reads the row the gallery renders, resolves the
# url it carries all the way down to the stored bytes, and asserts those bytes are
# the SECOND image. Everything between (the doc upsert preserving the field, the
# uploads row, the DTO) is exercised rather than assumed, because a card can lie at
# any one of those layers while every layer above it looks correct.
#
# What that test proved about the two failure modes worth ruling out:
#   * NOT a stable-key overwrite. Each capture mints its own uploads row, so
#     ``preview_image_url`` takes a NEW value every time and the old url still
#     resolves to the old bytes (asserted). No cache — CDN or browser — can serve
#     stale art behind a url the client has never fetched.
#   * NOT a skip-if-present no-op. ``take_site_screenshot`` has no "already has a
#     preview" guard, and the mutation plan (tests/mutations/site_preview_refresh.json)
#     adds one to watch this test fail.
# The remaining staleness vector is the PAGE rather than the picture — Browser
# Rendering fetches through Cloudflare's edge, which may still hold the
# pre-republish document — so each capture appends a unique cache-busting param.
# The tests pin that the param is sent and differs per capture. They CANNOT pin
# that Cloudflare honours it; nothing in a unit test can.
#
# And the asymmetry SC-3 introduces, which is the thing most at risk of being
# "tidied" later: the deploy-triggered capture may never raise, while the manual
# refresh must. Same broken Cloudflare, two opposite behaviours, pinned in one test
# (``test_the_deploy_path_swallows_the_failure_the_manual_path_reports``).
#
# Mutations that break these tests (run via scripts/mutate.py — a gate nobody has
# watched fail is not a gate): making ``take_site_screenshot`` return early when a
# preview already exists breaks the republish test; dropping the cache-buster from
# the capture url breaks the fresh-url test; calling ``safe_take_site_screenshot``
# from ``refresh_site_preview`` breaks the reports-failure test; returning the
# previous url instead of raising when a capture declines breaks the
# nothing-to-photograph test.
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.sites import draft_markup as draft_markup_mod
from pocketpaw_ee.sites import screenshot as screenshot_mod
from pocketpaw_ee.sites import service as sites_service


def _png(red: int, green: int, blue: int) -> bytes:
    """A real, minimal 1x1 PNG of the given colour.

    Real because the upload pipeline sniffs magic bytes and would reject a
    placeholder — a fake payload would make the happy path pass for the wrong
    reason. Generated rather than pasted as base64 so that "these two images are
    different" is visible in the source instead of being two opaque blobs.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit truecolour
    idat = zlib.compress(bytes([0, red, green, blue]))  # one row, no filter
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# The design before the edit, and the design after it. The whole slice is the
# question of which one the card ends up showing.
_BEFORE = _png(255, 0, 0)
_AFTER = _png(0, 0, 255)


class _ShotSequence:
    """A Cloudflare client that hands back a DIFFERENT image on each capture.

    Recording both ``url`` and ``html`` matters: which one a call arrives with is
    how a test tells the live path (photograph the deployed page) from the draft
    path (photograph the markup) without reaching inside either.
    """

    def __init__(self, *results: bytes | Exception) -> None:
        self._results = list(results) or [_BEFORE]
        self.calls: list[dict] = []

    async def capture_screenshot(self, *, url=None, html=None, **_kw):
        self.calls.append({"url": url, "html": html})
        # The last result repeats, so a test that only cares about one image does
        # not have to count captures.
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True


async def _publish():
    """Publish pocket-1 live. Called twice by the republish test — the Site doc is
    upserted on the stable per-(workspace, pocket) id, so the second call is a real
    republish of the same site at the same url, not a second site."""
    return await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pocket-1",
        ripple_spec={"type": "container"},
        theme={},
        name="Brew and Co",
        preview=False,
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=(lambda site_id, project_dir: f"http://localhost/{site_id}"),
    )


@pytest.fixture
def published_url(monkeypatch):
    """Give the Workers-for-Platforms publish a public address. Without
    PAW_CF_SITES_DOMAIN a WfP deploy stamps ``url=""`` and the capture correctly
    skips a site with no page to photograph — a test that wants to prove a capture
    RAN has to configure the domain or it passes for the wrong reason."""
    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "paw-sites.test")


@pytest.fixture
def uploads_in_tmp(monkeypatch, tmp_path):
    """Land stored screenshots under the test's tmp dir rather than the developer's
    real ~/.pocketpaw. Returns the storage root so a test can read the bytes back.

    Patching ``Path.home`` alone is NOT enough, and the reason is worth knowing
    because it makes these tests silently useless on a configured machine.
    ``_store_screenshot`` builds its adapter through
    ``pocketpaw.uploads.factory.build_adapter``, which calls ``load_dotenv()`` —
    so a developer whose ``.env`` sets ``POCKETPAW_UPLOAD_ADAPTER=s3`` uploads the
    screenshot to the REAL bucket. The bytes never touch ``tmp_path``, and the
    read-back below fails with FileNotFoundError pointing at a path that looks
    correct.

    Worse, it fails in the direction that flatters us: the assertion is the only
    thing distinguishing url rotation from an overwrite, so on an s3-configured box
    the proof of this slice does not run at all. Pinning the adapter keeps the test
    measuring the property it claims to measure, wherever it runs."""
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path / ".pocketpaw" / "uploads"


def _run_captures_inline(monkeypatch) -> list:
    """Make the fire-and-forget capture run on the publish's own loop and hand back
    the tasks, so a test can await the background work instead of racing it."""
    ran: list = []

    def _inline(coro):
        import asyncio

        ran.append(asyncio.get_running_loop().create_task(coro))

    monkeypatch.setattr(screenshot_mod, "_default_screenshot_scheduler", _inline)
    return ran


async def _card_image_url() -> str | None:
    """The preview url on the row the gallery actually renders — read through
    ``list_for_workspace`` rather than off the doc, so the DTO is in the path."""
    rows = await sites_service.list_for_workspace("ws1")
    assert len(rows) == 1, f"expected one site, got {len(rows)}"
    return rows[0].preview_image_url


async def _stored_bytes(preview_url: str, root: Path) -> bytes:
    """Resolve ``/api/v1/uploads/{id}`` to the bytes actually on disk.

    This is what makes the republish test a proof rather than an assertion: the
    card's url is followed through the uploads row to the stored blob, so "the card
    shows the new design" is checked against the image itself.
    """
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    file_id = preview_url.rsplit("/", 1)[-1]
    rec = await MongoFileStore().get_scoped(file_id, workspace="ws1")
    assert rec is not None, f"no uploads row behind {preview_url}"
    return (root / rec.storage_key).read_bytes()


# --------------------------------------------------------------------------- #
# The slice: a republish changes the card
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_republish_replaces_the_card_art_with_the_new_design(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """THE SLICE, end to end. Publish, then republish with a different design, and
    the image behind the gallery card is the SECOND design.

    Deliberately proved by resolving the card's url down to the stored bytes. Every
    weaker check passes on a broken implementation: a changed url proves nothing
    about what it points at, and a mock call count proves nothing about what was
    persisted.

    Mutation: give ``take_site_screenshot`` an early return when the site already
    has a ``preview_image_url`` and this fails — the card keeps the old design.
    """
    cf = _ShotSequence(_BEFORE, _AFTER)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    await _publish()
    for task in ran:
        await task
    ran.clear()
    before = await _card_image_url()

    # The site is edited and shipped again: same pocket, same site, same url.
    await _publish()
    for task in ran:
        await task
    after = await _card_image_url()

    # The republish really did re-shoot — otherwise this passes on a site that was
    # simply photographed once.
    assert len(cf.calls) == 2

    assert before and after
    assert after != before, "the card is still pointing at the first capture"
    assert await _stored_bytes(after, uploads_in_tmp) == _AFTER

    # The old url still resolves to the old bytes: this is url rotation, not an
    # overwrite behind a stable key — so no CDN or browser cache is in a position
    # to serve the previous design.
    assert await _stored_bytes(before, uploads_in_tmp) == _BEFORE


@pytest.mark.asyncio
async def test_a_republish_photographs_the_same_site_not_a_second_one(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """The republish must land on the SAME Site doc. If a publish inserted a fresh
    row the gallery would grow a duplicate card and the "stale art" symptom would
    have a completely different cause — worth pinning next to the test above, which
    reads ``rows[0]`` and would otherwise be reading an arbitrary row."""
    cf = _ShotSequence(_BEFORE, _AFTER)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    first = await _publish()
    second = await _publish()
    for task in ran:
        await task

    assert str(first.id) == str(second.id)
    assert len(await sites_service.list_for_workspace("ws1")) == 1


@pytest.mark.asyncio
async def test_each_capture_asks_for_a_url_no_cache_has_seen(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """The picture is fresh (the test above); the PAGE is what could still be stale.
    Browser Rendering fetches through Cloudflare's edge, which can answer with the
    document that was there before the deploy — and the card would then show a
    brand-new screenshot of the old design.

    This pins that each capture addresses a distinct url on the site's own address.
    Whether Cloudflare honours the cache-buster is not testable here.

    Mutation: pass ``url=url`` instead of ``url=_shot_url(url)`` and this fails.
    """
    cf = _ShotSequence(_BEFORE, _AFTER)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    await _publish()
    for task in ran:
        await task

    assert len(cf.urls) == 2
    assert cf.urls[0] != cf.urls[1], "two captures asked the edge for the same url"
    for url in cf.urls:
        assert url.startswith(site.url), f"capture left the site's own address: {url}"
        assert screenshot_mod._SHOT_PARAM in url


def test_the_cache_buster_respects_a_url_that_already_has_a_query_string():
    """A site reached with query params (a campaign link on a custom domain) must
    still get a VALID url, not a second ``?``."""
    out = screenshot_mod._shot_url("https://brew.example.test/?utm_source=x")
    assert out.startswith("https://brew.example.test/?utm_source=x&_paw_shot=")
    assert out.count("?") == 1


# --------------------------------------------------------------------------- #
# The manual refresh
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_manual_refresh_recaptures_a_live_site_and_returns_the_new_image(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """The affordance itself: with no republish and no edit, ask for a new picture
    and get one. This is what fixes a card whose capture failed at deploy time —
    previously the only remedy was republishing an unchanged site."""
    cf = _ShotSequence(_BEFORE, _AFTER)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    for task in ran:
        await task
    before = await _card_image_url()

    out = await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(site.id))

    assert out.site_id == str(site.id)
    assert out.preview_image_url.startswith("/api/v1/uploads/")
    assert out.preview_image_url != before
    # The returned url is the NEW image, and the card agrees with the response.
    assert await _stored_bytes(out.preview_image_url, uploads_in_tmp) == _AFTER
    assert await _card_image_url() == out.preview_image_url


@pytest.mark.asyncio
async def test_a_manual_refresh_of_a_draft_shoots_its_markup_not_a_page(
    beanie_test_db, monkeypatch, uploads_in_tmp
):
    """A draft has no url, so there is no page to photograph — the refresh must
    route to the markup capture rather than skipping, or a never-published site
    could never gain a card image on demand."""
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    oid = ObjectId()
    await _SiteDoc(
        id=oid,
        workspace="ws1",
        pocket_id="pocket-draft",
        owner="u1",
        name="Not shipped yet",
        script_name=str(oid),
        deployed=False,
        url="",
        signed_key="k",
    ).insert()

    async def _markup(_site):
        return "<html><body>draft</body></html>"

    monkeypatch.setattr(draft_markup_mod, "build_draft_markup", _markup)
    cf = _ShotSequence(_AFTER)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)

    out = await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(oid))

    assert out.preview_image_url.startswith("/api/v1/uploads/")
    # The draft path posts an ``html`` body and no url — which is how we know it
    # did not try to photograph a page that does not exist.
    assert cf.calls == [{"url": None, "html": "<html><body>draft</body></html>"}]


@pytest.mark.asyncio
async def test_a_manual_refresh_will_not_photograph_another_workspaces_site(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """Tenant scoping. A cross-workspace site id is a 404 and — the part that would
    be a real leak — no render is attempted, so a caller cannot use this endpoint to
    make us fetch a page they have no claim to."""
    cf = _ShotSequence(_BEFORE)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    for task in ran:
        await task
    cf.calls.clear()

    with pytest.raises(NotFound):
        await sites_service.refresh_site_preview(workspace_id="ws-other", site_id=str(site.id))

    assert cf.calls == []


@pytest.mark.asyncio
async def test_a_manual_refresh_that_finds_nothing_to_photograph_says_so(
    beanie_test_db, monkeypatch, uploads_in_tmp
):
    """A draft whose markup is not buildable is the NORMAL case, not an exotic one
    (a never-built ripple pocket is deliberately not built just for a thumbnail).
    The refresh must say that plainly — a 200 carrying the previous url would report
    success for a refresh that did not happen.

    Mutation: return the site's existing ``preview_image_url`` instead of raising
    and this fails.
    """
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    oid = ObjectId()
    await _SiteDoc(
        id=oid,
        workspace="ws1",
        pocket_id="pocket-draft",
        owner="u1",
        name="Nothing to shoot",
        script_name=str(oid),
        deployed=False,
        url="",
        signed_key="k",
        preview_image_url="/api/v1/uploads/stale",
    ).insert()

    async def _no_markup(_site):
        return ""

    monkeypatch.setattr(draft_markup_mod, "build_draft_markup", _no_markup)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: _ShotSequence(_AFTER))

    with pytest.raises(ValidationError) as exc:
        await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(oid))
    assert exc.value.code == "sites.preview_unavailable"


@pytest.mark.asyncio
async def test_the_deploy_path_swallows_the_failure_the_manual_path_reports(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """THE ASYMMETRY, in one test so nobody unifies the two by accident.

    Same broken Cloudflare, two opposite obligations. A deploy-triggered capture may
    never raise: the site is already live and serving, and a picture is not worth
    failing a publish over. A manual refresh MUST raise: a person pressed a button
    and is waiting, and handing them back a 200 with the same stale url they were
    trying to replace is a lie.

    Mutation: call ``safe_take_site_screenshot`` from ``refresh_site_preview`` and
    the second half fails — the refresh reports success having captured nothing.
    """
    boom = ValidationError("sites.cloudflare_error", "browser rendering is down")
    cf = _ShotSequence(boom)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    # The publish survives a capture that raises on its own stack.
    site = await _publish()
    for task in ran:
        await task
    assert site.deployed is True
    assert cf.calls, "the capture was never attempted — this would pass vacuously"
    assert await _card_image_url() is None

    # The same failure, asked for explicitly, reaches the caller.
    with pytest.raises(ValidationError) as exc:
        await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(site.id))
    assert exc.value.code == "sites.cloudflare_error"


@pytest.mark.asyncio
async def test_a_failed_manual_refresh_leaves_the_existing_card_alone(
    beanie_test_db, monkeypatch, published_url, uploads_in_tmp
):
    """Reporting failure must not also DESTROY the picture the site already had. A
    card showing a slightly stale design beats a card showing nothing."""
    cf = _ShotSequence(_BEFORE)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    ran = _run_captures_inline(monkeypatch)

    site = await _publish()
    for task in ran:
        await task
    good = await _card_image_url()
    assert good

    monkeypatch.setattr(
        sites_service,
        "_cf_client",
        lambda: _ShotSequence(ValidationError("sites.cloudflare_error", "quota exceeded")),
    )
    with pytest.raises(ValidationError):
        await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(site.id))

    assert await _card_image_url() == good
