# Tests for the stock-image search tool (Paw Sites imagery).
# Created: 2026-07-04 (feat/paw-sites-stock-imagery).
#
# httpx.MockTransport stands in for the live Pexels / Unsplash APIs (no network,
# via the stock_images._TRANSPORT injection seam). Asserts:
#   * Pexels + Unsplash responses normalize to the {url, alt, credit, provider}
#     shape and `url` is the CDN rendition.
#   * Unsplash's download_location trigger fires on search (API-terms compliance).
#   * zero configured keys → [] (site ships text-only, no regression).
#   * a provider error (500 / timeout) degrades to [] rather than raising.
#   * StockImageTool.execute returns JSON on hit and a graceful message on empty.

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pocketpaw.tools.builtin import stock_images
from pocketpaw.tools.builtin.stock_images import StockImageTool, search_stock_images

_PEXELS_BODY = {
    "photos": [
        {
            "src": {"large": "https://images.pexels.com/photo-1/large.jpg", "original": "x"},
            "alt": "modern dental office",
            "photographer": "Jane Doe",
            "url": "https://www.pexels.com/photo/1",
            "width": 940,
            "height": 650,
        }
    ]
}

_UNSPLASH_BODY = {
    "results": [
        {
            "urls": {"regular": "https://images.unsplash.com/photo-2?w=1080"},
            "alt_description": "bakery bread",
            "user": {"name": "John Roe", "links": {"html": "https://unsplash.com/@john"}},
            "links": {"download_location": "https://api.unsplash.com/photos/2/download"},
            "width": 1080,
            "height": 720,
        }
    ]
}


def _settings(pexels=None, unsplash=None):
    return MagicMock(pexels_api_key=pexels, unsplash_access_key=unsplash)


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _reset_transport():
    """Ensure no transport leaks between tests."""
    yield
    stock_images._TRANSPORT = None


class TestSearchStockImages:
    def test_no_keys_returns_empty(self):
        with patch("pocketpaw.tools.builtin.stock_images.get_settings", return_value=_settings()):
            assert search_stock_images("anything") == []

    def test_empty_query_returns_empty(self):
        with patch(
            "pocketpaw.tools.builtin.stock_images.get_settings",
            return_value=_settings(pexels="k"),
        ):
            assert search_stock_images("   ") == []

    def test_pexels_normalizes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "api.pexels.com" in str(request.url)
            assert request.headers["Authorization"] == "pexels-key"
            return httpx.Response(200, json=_PEXELS_BODY)

        stock_images._TRANSPORT = _transport(handler)
        with patch(
            "pocketpaw.tools.builtin.stock_images.get_settings",
            return_value=_settings(pexels="pexels-key"),
        ):
            results = search_stock_images("modern dental office", count=1)

        assert len(results) == 1
        r = results[0]
        assert r["url"] == "https://images.pexels.com/photo-1/large.jpg"
        assert r["provider"] == "pexels"
        assert r["alt"] == "modern dental office"
        assert "Jane Doe" in r["credit"] and "Pexels" in r["credit"]

    def test_unsplash_normalizes_and_fires_download_trigger(self):
        triggered: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "search/photos" in url:
                assert request.headers["Authorization"] == "Client-ID unsplash-key"
                return httpx.Response(200, json=_UNSPLASH_BODY)
            if "download" in url:
                triggered.append(url)
                return httpx.Response(200, json={"url": "ignored"})
            return httpx.Response(404)

        stock_images._TRANSPORT = _transport(handler)
        with patch(
            "pocketpaw.tools.builtin.stock_images.get_settings",
            return_value=_settings(unsplash="unsplash-key"),
        ):
            results = search_stock_images("bakery bread", count=1)

        assert len(results) == 1
        r = results[0]
        assert r["url"] == "https://images.unsplash.com/photo-2?w=1080"
        assert r["provider"] == "unsplash"
        assert "John Roe" in r["credit"] and "Unsplash" in r["credit"]
        # Unsplash API terms: the download_location must be triggered on use.
        assert triggered == ["https://api.unsplash.com/photos/2/download"]

    def test_provider_error_degrades_to_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        stock_images._TRANSPORT = _transport(handler)
        with patch(
            "pocketpaw.tools.builtin.stock_images.get_settings",
            return_value=_settings(pexels="k"),
        ):
            # Must not raise — a provider outage never fails the caller's build.
            assert search_stock_images("anything") == []

    def test_both_providers_split_count(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "pexels" in str(request.url):
                return httpx.Response(200, json=_PEXELS_BODY)
            if "search/photos" in str(request.url):
                return httpx.Response(200, json=_UNSPLASH_BODY)
            return httpx.Response(200, json={})  # download trigger

        stock_images._TRANSPORT = _transport(handler)
        with patch(
            "pocketpaw.tools.builtin.stock_images.get_settings",
            return_value=_settings(pexels="pk", unsplash="uk"),
        ):
            results = search_stock_images("office", count=2)

        providers = {r["provider"] for r in results}
        assert providers == {"pexels", "unsplash"}


class TestStockImageTool:
    @pytest.fixture
    def tool(self):
        return StockImageTool()

    def test_name_and_schema(self, tool):
        assert tool.name == "search_stock_images"
        assert tool.trust_level == "standard"
        assert "query" in tool.parameters["required"]

    async def test_execute_returns_json_on_hit(self, tool):
        with patch(
            "pocketpaw.tools.builtin.stock_images.search_stock_images",
            return_value=[{"url": "u", "alt": "a", "credit": "c", "provider": "pexels"}],
        ):
            out = await tool.execute(query="office")
        parsed = json.loads(out)
        assert parsed[0]["url"] == "u"

    async def test_execute_graceful_when_empty(self, tool):
        with patch(
            "pocketpaw.tools.builtin.stock_images.search_stock_images",
            return_value=[],
        ):
            out = await tool.execute(query="office")
        assert "without imagery" in out.lower()
