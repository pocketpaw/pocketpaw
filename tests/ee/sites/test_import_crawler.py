# tests/ee/sites/test_import_crawler.py — SI-5 (feat/sites-import-crawler): the
# same-site URL crawler behind POST /sites/import/from-url.
#
# Created 2026-07-23. All network is mocked (httpx.MockTransport + a fake DNS
# resolver) — nothing here ever opens a real socket. Covers:
#   * SSRF floors: literal loopback / RFC1918 / link-local / metadata / CGNAT /
#     v4-mapped-v6 seeds rejected at validation; a hostname RESOLVING private
#     rejected with zero requests made; a redirect to a private host or to a
#     non-http scheme rejected MID-CHAIN; ports beyond 80/443 rejected; file://
#     and credentialed URLs rejected. The endpoint 422s forbidden seeds before
#     anything is minted.
#   * Connection pinning: the socket-level request goes to the RESOLVED IP while
#     the Host header carries the original hostname (TOCTOU/rebinding closed).
#   * Crawl semantics: BFS depth + page caps, robots.txt disallow honored (with
#     the skipped counter), exact-host enforcement (cross-origin refs counted,
#     never fetched), URL-path traversal sanitized through the shared
#     safe-rel-path rule, total byte budget aborts the crawl.
#   * The wired import: crawl_site_from_url end to end over a 2-page fixture
#     site (css + img) → the SAME import pipeline as zip (publish faked) with
#     the right FileMap split, absolute same-origin URLs rewritten
#     root-relative, crawl stats + status on the persisted import_report, the
#     harvested source persisted on the pocket, and failure modes (unreachable
#     seed) landing as a safe ``status:"failed"`` report — never a traceback.
from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import import_service, url_crawler
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.url_crawler import CrawlBudgetExceeded, CrawlError

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

_PUBLIC_IPS = {
    "example.com": ["93.184.216.34"],
    "cdn.example": ["203.0.113.7"],
    "other.example": ["203.0.113.8"],
}


def _resolver(table: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError(f"no DNS for {host}")
        return table[host]

    return resolve


def _transport(pages: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]):
    """MockTransport routing on (Host header, path) — the pinned request carries
    the IP in the URL, so the ORIGINAL host is only in the Host header."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.headers.get("host", ""), request.url.path)
        return pages.get(key, httpx.Response(404, content=b"not found"))

    return httpx.MockTransport(handler)


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())


async def _crawl(pages, *, table=None, seen=None, byte_cap=1024 * 1024, **kw):
    seen = seen if seen is not None else []
    return await url_crawler.crawl_site(
        kw.pop("url", "https://example.com/"),
        total_byte_cap=byte_cap,
        transport=_transport(pages, seen),
        resolver=_resolver(table or _PUBLIC_IPS),
        politeness_delay=0,
        **kw,
    )


# --------------------------------------------------------------------------- #
# SSRF floors — seed validation (no socket, no DNS)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "seed",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/site",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
    ],
)
def test_non_public_literal_ip_seeds_rejected(seed):
    with pytest.raises(ValidationError) as exc:
        url_crawler.validate_seed_url(seed)
    assert exc.value.code == "sites.import_url_forbidden"


@pytest.mark.parametrize(
    "seed, code",
    [
        ("http://example.com:8080/", "sites.import_url_forbidden"),
        ("https://example.com:8443/", "sites.import_url_forbidden"),
        ("file:///etc/passwd", "sites.import_url_invalid"),
        ("gopher://example.com/", "sites.import_url_invalid"),
        ("http://user:pass@example.com/", "sites.import_url_forbidden"),
    ],
)
def test_bad_scheme_port_credentials_rejected(seed, code):
    with pytest.raises(ValidationError) as exc:
        url_crawler.validate_seed_url(seed)
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_hostname_resolving_private_makes_zero_requests():
    """A clean-looking hostname whose DNS answer is private is rejected BEFORE
    any request is sent — the resolve-check-pin order closes the rebinding hole."""
    seen: list[httpx.Request] = []
    with pytest.raises(CrawlError):
        await _crawl({}, table={"example.com": ["10.0.0.5"]}, seen=seen)
    assert seen == []  # nothing was ever fetched


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answer_rejected():
    seen: list[httpx.Request] = []
    with pytest.raises(CrawlError):
        await _crawl({}, table={"example.com": ["93.184.216.34", "10.0.0.5"]}, seen=seen)
    assert seen == []


@pytest.mark.asyncio
async def test_redirect_to_private_host_rejected_mid_chain():
    """Every redirect hop re-runs DNS + IP validation: a 302 bounce to an
    internal host dies without the internal host ever being contacted."""
    seen: list[httpx.Request] = []
    pages = {
        ("example.com", "/"): httpx.Response(
            302, headers={"location": "http://internal.example/admin"}
        ),
    }
    table = dict(_PUBLIC_IPS) | {"internal.example": ["192.168.1.1"]}
    with pytest.raises(CrawlError) as exc:
        await _crawl(pages, table=table, seen=seen)
    assert exc.value.code == "sites.import_crawl_seed_unreachable"
    assert all(r.headers.get("host") != "internal.example" for r in seen)


@pytest.mark.asyncio
async def test_redirect_to_non_http_scheme_rejected():
    pages = {
        ("example.com", "/"): httpx.Response(302, headers={"location": "file:///etc/passwd"}),
    }
    with pytest.raises(CrawlError) as exc:
        await _crawl(pages)
    assert exc.value.code == "sites.import_crawl_seed_unreachable"


@pytest.mark.asyncio
async def test_connection_is_pinned_to_resolved_ip():
    """The wire request targets the RESOLVED IP; the hostname rides only in the
    Host header — a second resolution can never swap the destination."""
    seen: list[httpx.Request] = []
    pages = {("example.com", "/"): _html("<html><body>hi</body></html>")}
    await _crawl(pages, seen=seen)
    page_requests = [r for r in seen if r.url.path == "/"]
    assert page_requests, "the seed page was never fetched"
    for request in page_requests:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"


# --------------------------------------------------------------------------- #
# crawl semantics
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bfs_respects_depth_cap():
    """A 6-deep link chain stops at depth 3 (seed = depth 0)."""
    pages = {
        ("example.com", "/"): _html('<a href="/d1">1</a>'),
        ("example.com", "/d1"): _html('<a href="/d2">2</a>'),
        ("example.com", "/d2"): _html('<a href="/d3">3</a>'),
        ("example.com", "/d3"): _html('<a href="/d4">4</a>'),
        ("example.com", "/d4"): _html('<a href="/d5">5</a>'),
    }
    result = await _crawl(pages)
    assert result.stats.pages_fetched == 4  # /, /d1, /d2, /d3
    assert "d3/index.html" in result.files
    assert "d4/index.html" not in result.files


@pytest.mark.asyncio
async def test_bfs_respects_page_cap(monkeypatch):
    monkeypatch.setattr(url_crawler, "MAX_CRAWL_PAGES", 2)
    pages = {
        ("example.com", "/"): _html('<a href="/a">a</a><a href="/b">b</a><a href="/c">c</a>'),
        ("example.com", "/a"): _html("<p>a</p>"),
        ("example.com", "/b"): _html("<p>b</p>"),
        ("example.com", "/c"): _html("<p>c</p>"),
    }
    result = await _crawl(pages)
    assert result.stats.pages_fetched == 2
    assert any("page cap" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_robots_disallow_honored():
    pages = {
        ("example.com", "/robots.txt"): httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"User-agent: *\nDisallow: /private\n",
        ),
        ("example.com", "/"): _html('<a href="/private">p</a><a href="/public">ok</a>'),
        ("example.com", "/private"): _html("<p>secret</p>"),
        ("example.com", "/public"): _html("<p>open</p>"),
    }
    result = await _crawl(pages)
    assert result.stats.skipped_by_robots == 1
    assert "public/index.html" in result.files
    assert "private/index.html" not in result.files


@pytest.mark.asyncio
async def test_cross_host_links_counted_never_fetched():
    seen: list[httpx.Request] = []
    pages = {
        ("example.com", "/"): _html(
            '<a href="https://other.example/page">x</a><img src="https://cdn.example/logo.png">'
        ),
    }
    result = await _crawl(pages, seen=seen)
    assert result.stats.cross_origin_refs == 2
    assert all(r.headers.get("host") == "example.com" for r in seen)
    assert any("cross-origin" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_url_path_traversal_sanitized():
    """A link whose decoded path traverses (..%2f..) is fetched-then-REFUSED a
    FileMap slot by the shared safe-rel-path rule — no key ever escapes root."""
    traversal = "/..%2f..%2fetc%2fpasswd"
    pages = {
        ("example.com", "/"): _html(f'<a href="{traversal}">evil</a>'),
        # MockTransport sees the DECODED path on request.url.path.
        ("example.com", "/../../etc/passwd"): _html("<p>gotcha</p>"),
    }
    result = await _crawl(pages)
    assert all(".." not in path for path in result.files)
    assert any("not importable" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_total_byte_budget_aborts_crawl():
    big = "<html><body>" + "A" * 4096 + "</body></html>"
    pages = {("example.com", "/"): _html(big)}
    with pytest.raises(CrawlBudgetExceeded):
        await _crawl(pages, byte_cap=512)


@pytest.mark.asyncio
async def test_assets_and_css_refs_harvested_same_origin_only():
    pages = {
        ("example.com", "/"): _html(
            "<html><head>"
            '<link rel="stylesheet" href="/css/site.css">'
            "</head><body>"
            '<img src="/img/logo.png">'
            '<script src="https://cdn.example/app.js"></script>'
            "</body></html>"
        ),
        ("example.com", "/css/site.css"): httpx.Response(
            200,
            headers={"content-type": "text/css"},
            content=b'body { background: url("/img/bg.png"); }',
        ),
        ("example.com", "/img/logo.png"): httpx.Response(
            200, headers={"content-type": "image/png"}, content=_PNG_BYTES
        ),
        ("example.com", "/img/bg.png"): httpx.Response(
            200, headers={"content-type": "image/png"}, content=_PNG_BYTES
        ),
    }
    result = await _crawl(pages)
    assert result.stats.assets_fetched == 3  # css + logo + the css-discovered bg
    assert {"css/site.css", "img/logo.png", "img/bg.png"} <= set(result.files)
    assert result.stats.cross_origin_refs == 1  # the cdn script, left as-is


# --------------------------------------------------------------------------- #
# the wired import — crawl_site_from_url end to end (publish faked)
# --------------------------------------------------------------------------- #

_WS = "ws_owner"
_USER = "user-test-1"

_INDEX = (
    "<html><head><title>Crawled Home</title>"
    '<link rel="stylesheet" href="/css/site.css"></head>'
    '<body><a href="https://example.com/about">about</a>'
    '<img src="/img/logo.png">'
    '<form action="https://old.example/submit"><input name="e"></form>'
    "</body></html>"
)
_ABOUT = "<html><head><title>Crawled About</title></head><body><p>hi</p></body></html>"

_SITE_PAGES = {
    ("example.com", "/"): _html(_INDEX),
    ("example.com", "/about"): _html(_ABOUT),
    ("example.com", "/css/site.css"): httpx.Response(
        200, headers={"content-type": "text/css"}, content=b"body{color:#111}"
    ),
    ("example.com", "/img/logo.png"): httpx.Response(
        200, headers={"content-type": "image/png"}, content=_PNG_BYTES
    ),
}


@pytest_asyncio.fixture
async def _fake_publish(beanie_test_db, monkeypatch) -> dict[str, Any]:
    """Fake sites_service.publish (a real one shells out to bun/the generator) —
    mirrors tests/ee/sites/test_import.py's fixture: record kwargs, flip the real
    draft Site doc to deployed."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    captured: dict[str, Any] = {}

    async def _publish(**kw):
        captured.update(kw)
        oid = sites_service._live_object_id(kw["workspace_id"], kw["pocket_id"])
        doc = await _SiteDoc.find_one({"_id": oid, "workspace": kw["workspace_id"]})
        assert doc is not None
        doc.script_name = str(oid)
        doc.deployed = True
        doc.url = f"http://127.0.0.1:9999/{oid}/"
        await doc.save()
        return doc

    monkeypatch.setattr(sites_service, "publish", _publish)
    return captured


async def _queue_import(monkeypatch, url: str = "https://example.com/") -> dict[str, str]:
    """Run the real import_from_url (plan stubbed, scheduler captured-and-closed)
    to mint the pocket + queued draft Site the crawl then fills."""
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="go"))
    monkeypatch.setattr(import_service, "_default_crawl_scheduler", lambda coro: coro.close())
    return await import_service.import_from_url(workspace_id=_WS, user_id=_USER, url=url)


@pytest.mark.asyncio
async def test_from_url_import_happy_path(_fake_publish, monkeypatch):
    """2-page fixture site with css + img: the crawl runs the SAME pipeline as
    the zip path — text/binary split, publish with the assets sideband, report
    persisted with crawl stats — and the harvested source lands on the pocket."""
    queued = await _queue_import(monkeypatch)
    seen: list[httpx.Request] = []
    doc = await import_service.crawl_site_from_url(
        workspace_id=_WS,
        user_id=_USER,
        site_id=queued["site_id"],
        url="https://example.com/",
        _transport=_transport(_SITE_PAGES, seen),
        _resolver=_resolver(_PUBLIC_IPS),
        _politeness_delay=0,
    )

    # The publish rode the html engine with the harvested source + assets.
    assert _fake_publish["engine"] == "html"
    assert _fake_publish["pattern"] == "imported"
    assert set(_fake_publish["source"]) == {"index.html", "about/index.html", "css/site.css"}
    assert _fake_publish["assets"] == {"img/logo.png": base64.b64encode(_PNG_BYTES).decode("ascii")}
    # Absolute same-origin links were rewritten root-relative.
    assert 'href="/about"' in _fake_publish["source"]["index.html"]
    assert "https://example.com" not in _fake_publish["source"]["index.html"]

    # The report persisted with crawl stats + the derived page/form scan.
    report = doc.import_report
    assert report["status"] == "imported"
    assert report["source_url"] == "https://example.com/"
    assert report["crawl"]["pages_fetched"] == 2
    assert report["crawl"]["assets_fetched"] == 2
    assert report["crawl"]["skipped_by_robots"] == 0
    assert report["crawl"]["bytes_fetched"] > 0
    titles = {p["path"]: p["title"] for p in report["pages"]}
    assert titles["index.html"] == "Crawled Home"
    assert titles["about/index.html"] == "Crawled About"
    assert report["forms"][0]["original_action"] == "https://old.example/submit"
    assert doc.deployed is True

    # The harvested source persisted on the POCKET (durable re-publish source).
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    pocket = await _PocketDoc.find_one({"workspace": _WS})
    assert pocket is not None
    assert set(pocket.source or {}) == {"index.html", "about/index.html", "css/site.css"}


@pytest.mark.asyncio
async def test_from_url_unreachable_seed_marks_failed_report(beanie_test_db, monkeypatch):
    """An unreachable seed never crashes the background task — the draft Site's
    report flips to a clear failed state with a SAFE message, no stack trace."""
    queued = await _queue_import(monkeypatch, url="https://example.com/gone")
    pages = {("example.com", "/gone"): httpx.Response(503, content=b"upstream exploded")}
    doc = await import_service.crawl_site_from_url(
        workspace_id=_WS,
        user_id=_USER,
        site_id=queued["site_id"],
        url="https://example.com/gone",
        _transport=_transport(pages, []),
        _resolver=_resolver(_PUBLIC_IPS),
        _politeness_delay=0,
    )
    assert doc.deployed is False
    assert doc.import_report["status"] == "failed"
    assert doc.import_report["error"]  # a clear, fixed message…
    assert "Traceback" not in doc.import_report["error"]  # …never a stack trace
    assert "upstream exploded" not in doc.import_report["error"]  # never raw upstream text


@pytest.mark.asyncio
async def test_from_url_byte_cap_marks_failed_report(beanie_test_db, monkeypatch):
    queued = await _queue_import(monkeypatch)
    big = _html("<html><body>" + "A" * 8192 + "</body></html>")
    monkeypatch.setattr(import_service, "MAX_IMPORT_UNCOMPRESSED_BYTES", 512)
    doc = await import_service.crawl_site_from_url(
        workspace_id=_WS,
        user_id=_USER,
        site_id=queued["site_id"],
        url="https://example.com/",
        _transport=_transport({("example.com", "/"): big}, []),
        _resolver=_resolver(_PUBLIC_IPS),
        _politeness_delay=0,
    )
    assert doc.import_report["status"] == "failed"
    assert "budget" in doc.import_report["error"]


# --------------------------------------------------------------------------- #
# endpoint-level SSRF: forbidden seeds 422 before anything is minted
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seed",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/intranet",
        "http://example.com:8080/",
        "http://user:pass@example.com/",
    ],
)
async def test_endpoint_rejects_forbidden_seed_with_422(beanie_test_db, monkeypatch, seed):
    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="go"))
    with pytest.raises(ValidationError):
        await import_service.import_from_url(workspace_id=_WS, user_id=_USER, url=seed)
    assert await _SiteDoc.find_one({"workspace": _WS}) is None
