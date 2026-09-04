# tests/ee/sites/test_import_design_brief.py — IR-2a (feat/sites-import-design-brief):
# REBUILD mode on POST /sites/import/from-url.
#
# Created 2026-09-04. Covers:
#   * The endpoint contract for both modes. ``mode="rebuild"`` returns 202
#     {brief_id, status:"queued", mode:"rebuild"} and mints NO pocket and NO Site
#     doc; the default (no mode sent) still mirrors, so the shipped client that
#     posts only ``url`` reads the same fields it always did.
#   * The design brief round-trips through persistence, and ``load_brief`` refuses
#     a version this build cannot read instead of defaulting fields away.
#   * The capture happy path: a fake harvest becomes a ready brief carrying the
#     source's own title, description and an ABSOLUTE favicon URL.
#   * Every capture failure lands as a readable ``failed`` status rather than
#     escaping into the event loop, which has nobody to return to.
#   * The regression this feature already tripped once: metadata is read with a
#     real parser, so a MINIFIED page whose attributes carry no quotes is read
#     correctly. A quote-assuming regex returns zero here with no error, which is
#     indistinguishable from a page that declares nothing.
#   * The seed page is found by its own PATH, not by assuming "index.html".
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import import_service

from tests.ee.sites.test_import import _assert_nothing_minted, _build_app

_PAGE = (
    "<!doctype html><html><head>"
    "<title>Rohit Kushwaha</title>"
    "<meta name='description' content='Full-Stack Engineer'>"
    "<link rel='icon' href='/favicon.ico'>"
    "<meta property='og:image' content='/og.png'>"
    "</head><body><h1>Hello</h1></body></html>"
)

# The same page as a minifier emits it: no quotes around any attribute value.
# This is the shape that broke metadata extraction once already.
_PAGE_MINIFIED = (
    "<!doctype html><html><head>"
    "<title>Rohit Kushwaha</title>"
    "<meta name=description content=Engineer>"
    "<link href=/favicon.ico rel=icon>"
    "</head><body></body></html>"
)


class _FakeCrawl:
    """Stand-in for ``url_crawler.CrawlResult`` — files plus warnings."""

    def __init__(self, files: dict[str, bytes], warnings: list[str] | None = None) -> None:
        self.files = files
        self.warnings = warnings or []


def _patch_crawl(monkeypatch, result: Any) -> None:
    """Replace the crawler with one that returns ``result`` (or raises it)."""

    async def _fake(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    from pocketpaw_ee.sites import url_crawler

    monkeypatch.setattr(url_crawler, "crawl_site", _fake)


# --------------------------------------------------------------------------- #
# The brief itself — shape and version discipline
# --------------------------------------------------------------------------- #


def test_brief_round_trips_and_refuses_a_version_it_cannot_read():
    from pocketpaw_ee.sites.design_brief import (
        BRIEF_VERSION,
        BriefVersionError,
        DesignBrief,
        load_brief,
    )

    brief = DesignBrief(source_url="https://example.test", captured_at=datetime.now(UTC))
    dumped = brief.model_dump(mode="json")
    assert load_brief(dumped).source_url == "https://example.test"

    # A brief outlives the capture that made it, so a mismatched version must fail
    # loudly. Defaulting a renamed field away produces a plausible site that is
    # wrong about its source and reports nothing.
    for bad in (BRIEF_VERSION + 1, BRIEF_VERSION - 1, "not-a-number", None):
        with pytest.raises(BriefVersionError):
            load_brief({**dumped, "version": bad})


def test_metadata_is_read_from_minified_markup():
    """The regression pin. A minifier strips attribute quotes; a quote-assuming
    pattern finds nothing and reports it as a page that declares nothing."""
    scan = import_service._MetaScan()
    scan.feed(_PAGE_MINIFIED)
    assert scan.title == "Rohit Kushwaha"
    assert scan.description == "Engineer"
    assert scan.favicon == "/favicon.ico"


def test_seed_page_is_found_by_its_own_path_not_index_html():
    """The crawler keys pages by path: ``/landing`` lands at ``landing/index.html``.
    Assuming ``index.html`` reads the wrong page on any non-root seed."""
    files = {
        "index.html": b"<title>Home</title>",
        "landing/index.html": b"<title>Landing</title>",
    }
    path, blob = import_service._seed_page_file("https://x.test/landing", files)
    assert path == "landing/index.html"
    assert b"Landing" in blob

    # Root seed still resolves to the root page.
    assert import_service._seed_page_file("https://x.test/", files)[0] == "index.html"


def test_brief_from_crawl_carries_the_sources_own_words():
    crawl = _FakeCrawl({"index.html": _PAGE.encode()}, warnings=["one crawl warning"])
    brief = import_service._brief_from_crawl("https://rohitk06.test/", crawl)

    assert brief.meta.title == "Rohit Kushwaha"
    assert brief.meta.description == "Full-Stack Engineer"
    # Resolved against the SOURCE url — a stored brief carries absolute addresses,
    # because the crawl's own rewrite made these site-relative.
    assert brief.meta.favicon_url == "https://rohitk06.test/favicon.ico"
    assert brief.meta.og_image_url == "https://rohitk06.test/og.png"
    assert "one crawl warning" in brief.warnings
    # Every other family is a later slice and lands empty rather than guessed.
    assert brief.sections == [] and brief.forms == [] and brief.assets == {}


def test_a_page_with_no_title_warns_rather_than_failing():
    crawl = _FakeCrawl({"index.html": b"<html><body><p>no head</p></body></html>"})
    brief = import_service._brief_from_crawl("https://x.test/", crawl)
    assert brief.meta.title == ""
    assert any("no title" in w for w in brief.warnings)


# --------------------------------------------------------------------------- #
# The endpoint — both modes, and what each one mints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rebuild_mode_queues_a_brief_and_mints_nothing(beanie_test_db, monkeypatch):
    """202 {brief_id, status:"queued", mode:"rebuild"}, the capture scheduled, and
    NO pocket / Site doc — the generating agent mints its own pocket, and
    pre-minting one here would route the run to refine against an html pocket."""
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    scheduled: list[object] = []

    def _capture(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(import_service, "_default_crawl_scheduler", _capture)
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/import/from-url",
            json={"url": "https://example.com/landing", "mode": "rebuild"},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["mode"] == "rebuild"
    assert body["status"] == "queued"
    assert body["brief_id"]
    assert body["site_id"] is None and body["pocket_id"] is None
    assert len(scheduled) == 1

    await _assert_nothing_minted()
    doc = await SiteDesignBrief.find_one({"workspace": "ws_owner"})
    assert doc is not None
    assert doc.status == "queued"
    assert doc.source_url == "https://example.com/landing"
    assert doc.brief == {}


@pytest.mark.asyncio
async def test_default_mode_still_mirrors(beanie_test_db, monkeypatch):
    """The shipped client posts only ``url``. It must keep getting a site, not a
    brief — the default flips in the PR that ships the picker, not before."""
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    monkeypatch.setattr(import_service, "_default_crawl_scheduler", lambda coro: coro.close())
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/import/from-url", json={"url": "https://example.com/landing"}
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["mode"] == "copy"
    assert body["site_id"] and body["pocket_id"]
    assert body["brief_id"] is None
    assert await SiteDesignBrief.find_one({"workspace": "ws_owner"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["notaurl", "ftp://example.com", "https://", "  "])
async def test_rebuild_rejects_a_bad_url_before_any_write(beanie_test_db, monkeypatch, bad):
    """Same SSRF + shape floors as the mirror path, and nothing is written first."""
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/import/from-url", json={"url": bad, "mode": "rebuild"})
    assert resp.status_code == 422, resp.text
    assert await SiteDesignBrief.find_one({"workspace": "ws_owner"}) is None
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_rebuild_rejects_a_loopback_target(beanie_test_db, monkeypatch):
    """The crawler's SSRF floor binds this path too: a literal private IP is a 422
    before a brief document exists."""
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/import/from-url",
            json={"url": "http://127.0.0.1/admin", "mode": "rebuild"},
        )
    assert resp.status_code == 422, resp.text
    assert await SiteDesignBrief.find_one({"workspace": "ws_owner"}) is None


# --------------------------------------------------------------------------- #
# The background capture — it must always land a status, never escape
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_capture_persists_a_ready_brief(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief
    from pocketpaw_ee.sites.design_brief import load_brief

    doc = SiteDesignBrief(
        workspace="ws_owner", owner="user-test-1", source_url="https://rohitk06.test/"
    )
    await doc.insert()
    _patch_crawl(monkeypatch, _FakeCrawl({"index.html": _PAGE.encode()}))

    await import_service.capture_design_brief(
        brief_id=str(doc.id), workspace_id="ws_owner", url="https://rohitk06.test/"
    )

    stored = await SiteDesignBrief.find_one({"_id": doc.id})
    assert stored is not None
    assert stored.status == "ready"
    assert stored.error == ""
    brief = load_brief(stored.brief)
    assert brief.meta.title == "Rohit Kushwaha"
    assert brief.source_url == "https://rohitk06.test/"


@pytest.mark.asyncio
async def test_a_failed_crawl_becomes_a_readable_status(beanie_test_db, monkeypatch):
    """The capture runs detached from the request that queued it, so a failure has
    nobody to raise to. It has to be legible on the document instead."""
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief
    from pocketpaw_ee.sites.url_crawler import CrawlError

    doc = SiteDesignBrief(workspace="ws_owner", owner="u", source_url="https://x.test/")
    await doc.insert()
    _patch_crawl(monkeypatch, CrawlError("seed is blocked by robots.txt"))

    await import_service.capture_design_brief(
        brief_id=str(doc.id), workspace_id="ws_owner", url="https://x.test/"
    )

    stored = await SiteDesignBrief.find_one({"_id": doc.id})
    assert stored is not None
    assert stored.status == "failed"
    assert "robots" in stored.error
    assert stored.brief == {}


@pytest.mark.asyncio
async def test_a_harvest_with_no_page_becomes_a_readable_status(beanie_test_db, monkeypatch):
    """A crawl that succeeds but yields nothing readable fails the same way — a
    status, not a traceback and not a silently empty brief."""
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    doc = SiteDesignBrief(workspace="ws_owner", owner="u", source_url="https://x.test/")
    await doc.insert()
    _patch_crawl(monkeypatch, _FakeCrawl({"style.css": b"body{}"}))

    await import_service.capture_design_brief(
        brief_id=str(doc.id), workspace_id="ws_owner", url="https://x.test/"
    )

    stored = await SiteDesignBrief.find_one({"_id": doc.id})
    assert stored is not None
    assert stored.status == "failed"
    assert stored.brief == {}


@pytest.mark.asyncio
async def test_the_background_wrapper_never_raises(beanie_test_db, monkeypatch):
    """``_run_design_capture`` is handed to the event loop. An unexpected error
    inside it must be logged and swallowed, never left to crash the loop."""

    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(import_service, "capture_design_brief", _boom)
    await import_service._run_design_capture(
        brief_id="deadbeefdeadbeefdeadbeef", workspace_id="ws_owner", url="https://x.test/"
    )


@pytest.mark.asyncio
async def test_capture_is_tenant_scoped(beanie_test_db, monkeypatch):
    """A brief id from another workspace is unknown here, not readable."""
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.models.site_design_brief import SiteDesignBrief

    doc = SiteDesignBrief(workspace="ws_other", owner="u", source_url="https://x.test/")
    await doc.insert()
    _patch_crawl(monkeypatch, _FakeCrawl({"index.html": _PAGE.encode()}))

    with pytest.raises(ValidationError):
        await import_service.capture_design_brief(
            brief_id=str(doc.id), workspace_id="ws_owner", url="https://x.test/"
        )
    stored = await SiteDesignBrief.find_one({"_id": doc.id})
    assert stored is not None and stored.status == "queued"
