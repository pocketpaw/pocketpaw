# Tests for the API v1 link-unfurl router (GET /api/v1/unfurl).
# Created: 2026-06-10 — covers the frozen wire contract: happy-path OG scrape,
#   twitter/<title> fallbacks, relative image+favicon resolved absolute,
#   SSRF-blocked URLs -> 400 unsafe_url, non-html/timeout/oversized -> 502
#   fetch_failed, the 15-min TTL cache (second call doesn't re-fetch), and a
#   YouTube-shaped fixture. Mocks the SSRF-safe fetch by pinning DNS to a
#   public IP and swapping IPPinningTransport's inner transport for an
#   httpx.MockTransport so the real safe_get_streamed (streaming, content-type
#   check, byte cap, final-URL tracking) is exercised end to end.
# 2026-09-26 (feat/unfurl-richer-previews) — TestRicherPreviews: crawler
#   User-Agent sent, 4xx pages -> 502, theme_color (hex only), large_image
#   from twitter:card / og:image:width, itemprop/image_src image fallbacks,
#   whitespace collapse + length caps.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pocketpaw.api.v1.unfurl as unfurl_module
from pocketpaw.api.v1.unfurl import router

# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

FULL_OG_HTML = """<!doctype html>
<html><head>
  <meta property="og:title" content="The Title">
  <meta property="og:description" content="A great description.">
  <meta property="og:image" content="https://cdn.example.com/img.png">
  <meta property="og:site_name" content="Example Site">
  <link rel="icon" href="https://example.com/favicon.ico">
  <title>Fallback Title</title>
</head><body><h1>hi</h1></body></html>
"""

TWITTER_FALLBACK_HTML = """<!doctype html>
<html><head>
  <meta name="twitter:title" content="Tweet Title">
  <meta name="twitter:description" content="Tweet desc.">
  <meta name="twitter:image" content="https://cdn.example.com/tw.png">
  <title>Doc Title</title>
</head><body></body></html>
"""

TITLE_ONLY_HTML = """<!doctype html>
<html><head><title>Just A Title</title></head><body>no og tags</body></html>
"""

RELATIVE_ASSETS_HTML = """<!doctype html>
<html><head>
  <meta property="og:title" content="Rel">
  <meta property="og:image" content="/assets/cover.jpg">
  <link rel="shortcut icon" href="favicon.png">
</head><body></body></html>
"""

NO_META_HTML = "<html><head></head><body><p>nothing here</p></body></html>"

YOUTUBE_HTML = """<!doctype html>
<html><head>
  <meta property="og:title" content="Rick Astley - Never Gonna Give You Up">
  <meta property="og:description" content="The official video">
  <meta property="og:image" content="https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg">
  <meta property="og:site_name" content="YouTube">
  <link rel="icon" href="https://www.youtube.com/s/desktop/favicon.ico">
  <title>Rick Astley - Never Gonna Give You Up - YouTube</title>
</head><body></body></html>
"""


# ---------------------------------------------------------------------------
# Fixtures + fetch-mock helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """The unfurl cache is module-level; isolate every test."""
    unfurl_module._cache.clear()
    yield
    unfurl_module._cache.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _mock_transport_returning(
    body: str | bytes,
    *,
    content_type: str = "text/html; charset=utf-8",
    status_code: int = 200,
    final_path: str = "/",
):
    """Build an httpx.MockTransport handler that returns a fixed response.

    Tracks call count on the returned handler's ``calls`` attribute.
    """
    content = body.encode("utf-8") if isinstance(body, str) else body
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(
            status_code,
            headers={"content-type": content_type},
            content=content,
        )

    return handler, state


# The SSRF-safe fetch is mocked by (1) pinning DNS to a public IP and (2)
# swapping IPPinningTransport's *inner* transport for an httpx.MockTransport.
# IPPinningTransport(transport=...) accepts an inner transport, but the patch
# on httpx.AsyncHTTPTransport (its default) is simpler and covers the loop's
# real construction path. Each test installs these patches inline.


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUnfurlContract:
    def test_full_og_tags(self, client):
        handler, state = _mock_transport_returning(FULL_OG_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/page"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://example.com/page"
        assert data["title"] == "The Title"
        assert data["description"] == "A great description."
        assert data["image"] == "https://cdn.example.com/img.png"
        assert data["site_name"] == "Example Site"
        assert data["favicon"] == "https://example.com/favicon.ico"

    def test_twitter_and_title_fallbacks(self, client):
        handler, _ = _mock_transport_returning(TWITTER_FALLBACK_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/t"})
        assert resp.status_code == 200
        data = resp.json()
        # og:title absent -> twitter:title wins over <title>
        assert data["title"] == "Tweet Title"
        assert data["description"] == "Tweet desc."
        assert data["image"] == "https://cdn.example.com/tw.png"

    def test_title_only_page(self, client):
        handler, _ = _mock_transport_returning(TITLE_ONLY_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/x"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Just A Title"
        assert data["description"] is None
        assert data["image"] is None
        assert data["site_name"] is None

    def test_all_null_metadata_is_valid_200(self, client):
        handler, _ = _mock_transport_returning(NO_META_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/empty"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://example.com/empty"
        assert data["title"] is None
        assert data["description"] is None
        assert data["image"] is None
        assert data["site_name"] is None
        assert data["favicon"] is None

    def test_relative_image_and_favicon_resolved_absolute(self, client):
        handler, _ = _mock_transport_returning(RELATIVE_ASSETS_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/dir/article"})
        assert resp.status_code == 200
        data = resp.json()
        # /assets/cover.jpg is root-relative; favicon.png is path-relative.
        assert data["image"] == "https://example.com/assets/cover.jpg"
        assert data["favicon"] == "https://example.com/dir/favicon.png"

    def test_youtube_shaped_fixture(self, client):
        handler, _ = _mock_transport_returning(YOUTUBE_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get(
                "/api/v1/unfurl",
                params={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "Never Gonna Give You Up" in data["title"]
        assert data["image"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
        assert data["site_name"] == "YouTube"

    def test_returns_final_url_after_redirect(self, client):
        def handler(request: httpx.Request) -> httpx.Response:
            if "start" in request.url.path:
                return httpx.Response(
                    302, headers={"location": "https://final.example.com/landing"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=FULL_OG_HTML.encode("utf-8"),
            )

        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/start"})
        assert resp.status_code == 200
        # The contract's url field is the final URL after redirects, and
        # relative metadata resolves against it.
        assert resp.json()["url"] == "https://final.example.com/landing"


class TestUnfurlInvalidAndUnsafe:
    def test_non_http_scheme_400_invalid_url(self, client):
        resp = client.get("/api/v1/unfurl", params={"url": "ftp://example.com/x"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_url"

    def test_no_host_400_invalid_url(self, client):
        resp = client.get("/api/v1/unfurl", params={"url": "not a url at all"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_url"

    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://localhost/admin",
            "http://127.0.0.1:8080/",
            "http://10.0.0.5/",
            "http://169.254.169.254/latest/meta-data/",
            "http://192.168.1.1/",
        ],
    )
    def test_internal_host_400_unsafe_url(self, client, bad_url):
        resp = client.get("/api/v1/unfurl", params={"url": bad_url})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "unsafe_url"

    def test_dns_resolves_to_private_ip_400_unsafe_url(self, client):
        # Hostname is public-looking; DNS resolution lands on a private IP.
        with patch(
            "pocketpaw.security.safe_fetch._resolve_public_ip",
            AsyncMock(side_effect=unfurl_module.BlockedURLError("non-public")),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "http://rebind.example.com/"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "unsafe_url"


class TestUnfurlFetchFailures:
    def test_non_html_content_type_502(self, client):
        handler, _ = _mock_transport_returning(b'{"k": "v"}', content_type="application/json")
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/data.json"})
        assert resp.status_code == 502
        assert resp.json()["detail"] == "fetch_failed"

    def test_timeout_502(self, client):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://slow.example.com/"})
        assert resp.status_code == 502
        assert resp.json()["detail"] == "fetch_failed"

    def test_dns_failure_502(self, client):
        with patch(
            "pocketpaw.security.safe_fetch._resolve_public_ip",
            AsyncMock(side_effect=unfurl_module.FetchFailedError("Could not resolve")),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "http://no-such-host.example.com/"})
        assert resp.status_code == 502
        assert resp.json()["detail"] == "fetch_failed"

    def test_oversized_body_is_cut_and_parsed(self, client):
        # A valid OG title near the top, then megabytes of filler. The fetch
        # cuts at ~512KB; the title (in <head>) survives, so we still get 200.
        filler = "<p>x</p>" * 200_000  # ~1.6 MB, well past the 512 KB cap
        big_html = (
            "<html><head><meta property='og:title' content='Big Page'>"
            "</head><body>" + filler + "</body></html>"
        )
        handler, _ = _mock_transport_returning(big_html)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            resp = client.get("/api/v1/unfurl", params={"url": "https://example.com/big"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Big Page"


class TestUnfurlCache:
    def test_second_call_within_ttl_does_not_refetch(self, client):
        handler, state = _mock_transport_returning(FULL_OG_HTML)
        mock_transport = httpx.MockTransport(handler)
        with (
            patch(
                "pocketpaw.security.safe_fetch._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch(
                "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            r1 = client.get("/api/v1/unfurl", params={"url": "https://example.com/cached"})
            r2 = client.get("/api/v1/unfurl", params={"url": "https://example.com/cached"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()
        # Transport was hit exactly once — the second call served from cache.
        assert state["calls"] == 1


def _unfurl_with(client, handler, url="https://example.com/p"):
    """GET /unfurl with DNS pinned public and the transport mocked."""
    mock_transport = httpx.MockTransport(handler)
    with (
        patch(
            "pocketpaw.security.safe_fetch._resolve_public_ip",
            AsyncMock(return_value="93.184.216.34"),
        ),
        patch(
            "pocketpaw.security.safe_fetch.httpx.AsyncHTTPTransport",
            return_value=mock_transport,
        ),
    ):
        return client.get("/api/v1/unfurl", params={"url": url})


def _page(head: str) -> str:
    return f"<!doctype html><html><head>{head}</head><body></body></html>"


class TestRicherPreviews:
    def test_sends_crawler_user_agent(self, client):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=FULL_OG_HTML.encode()
            )

        resp = _unfurl_with(client, handler)
        assert resp.status_code == 200
        assert "PocketPawBot" in seen["ua"]
        assert "facebookexternalhit" in seen["ua"]

    @pytest.mark.parametrize("status", [403, 404, 500])
    def test_error_status_page_is_fetch_failed(self, client, status):
        handler, _ = _mock_transport_returning(TITLE_ONLY_HTML, status_code=status)
        resp = _unfurl_with(client, handler)
        assert resp.status_code == 502
        assert resp.json()["detail"] == "fetch_failed"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("#5865F2", "#5865f2"),
            ("#fff", "#fff"),
            ("red", None),
            ("#12345", None),
            ("#fff;background:url(x)", None),
        ],
    )
    def test_theme_color_hex_only(self, client, value, expected):
        html = _page(
            f'<meta property="og:title" content="T"><meta name="theme-color" content="{value}">'
        )
        handler, _ = _mock_transport_returning(html)
        assert _unfurl_with(client, handler).json()["theme_color"] == expected

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            ('<meta name="twitter:card" content="summary_large_image">', True),
            ('<meta name="twitter:card" content="summary">', False),
            ('<meta property="og:image:width" content="200">', False),
            ('<meta property="og:image:width" content="1200">', True),
            ("", True),
        ],
    )
    def test_large_image(self, client, extra, expected):
        html = _page(f'<meta property="og:image" content="/i.png">{extra}')
        handler, _ = _mock_transport_returning(html)
        assert _unfurl_with(client, handler).json()["large_image"] is expected

    def test_large_image_null_without_image(self, client):
        handler, _ = _mock_transport_returning(TITLE_ONLY_HTML)
        data = _unfurl_with(client, handler).json()
        assert data["image"] is None
        assert data["large_image"] is None

    def test_itemprop_image_fallback(self, client):
        html = _page('<meta itemprop="image" content="/logo.png"><title>G</title>')
        handler, _ = _mock_transport_returning(html)
        assert _unfurl_with(client, handler).json()["image"] == "https://example.com/logo.png"

    def test_link_image_src_fallback(self, client):
        html = _page('<link rel="image_src" href="https://cdn.example.com/s.jpg"><title>S</title>')
        handler, _ = _mock_transport_returning(html)
        assert _unfurl_with(client, handler).json()["image"] == "https://cdn.example.com/s.jpg"

    def test_whitespace_collapsed_and_capped(self, client):
        long_desc = "word " * 400
        html = _page(
            '<meta property="og:title" content="  Hello\t\t   world  ">'
            f'<meta property="og:description" content="{long_desc}">'
        )
        handler, _ = _mock_transport_returning(html)
        data = _unfurl_with(client, handler).json()
        assert data["title"] == "Hello world"
        assert len(data["description"]) <= 600
        assert data["description"].endswith("…")
