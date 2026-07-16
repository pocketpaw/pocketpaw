# tests/ee/agent/test_icons_mcp_server/test_mcp_tool.py
# Created: 2026-07-06 (feat/sites-crew-icons, SC-6) — coverage for the in-process
# ``pocketpaw_icons`` MCP server. Mirrors the stock_images / sites test layout:
# registration assertions (server name, tool id namespacing, build shape,
# provider allowlist publication) plus per-handler tests that inject an
# httpx.MockTransport (NO live network) and inspect the MCP envelope the SDK
# returns to the agent — happy path, empty query, and provider error fail-soft.
"""MCP server registration + handler tests for the Iconify icon-search tool."""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.agent.mcp_servers import icons as icons_mcp  # noqa: E402,I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payload(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``. Decode it so
    the tests can assert on dict fields without re-encoding."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


def _mock_transport(handler) -> httpx.MockTransport:
    """Wrap a request handler in an httpx.MockTransport for injection into
    ``icons._TRANSPORT`` so the provider call never hits the network."""
    return httpx.MockTransport(handler)


@pytest.fixture
def restore_transport():
    """Ensure the module-level transport seam is reset after each test."""
    original = icons_mcp._TRANSPORT
    yield
    icons_mcp._TRANSPORT = original


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestIconsMcpServerRegistration:
    def test_server_name_and_tool_id_namespacing(self) -> None:
        assert icons_mcp.SERVER_NAME == "pocketpaw_icons"
        # The tool id must use the exact ``mcp__<server>__<tool>`` form so the
        # Claude Code allowlist machinery matches it.
        assert icons_mcp.SEARCH_ICONS_TOOL_ID == "mcp__pocketpaw_icons__search_icons"
        assert icons_mcp.ICON_TOOL_IDS == (icons_mcp.SEARCH_ICONS_TOOL_ID,)

    def test_extension_provider_advertises_tool_id(self) -> None:
        """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
        allowlist loop — the search tool id must come through it."""
        from pocketpaw_ee.extensions import CloudIconsMcpProvider

        advertised = CloudIconsMcpProvider().tool_ids()
        assert list(icons_mcp.ICON_TOOL_IDS) == advertised

    def test_provider_build_server_matches_shape(self) -> None:
        """The provider's ``build_server`` returns ``(name, server)`` when the
        Claude Agent SDK is installed (the ee group), or ``None`` otherwise."""
        from pocketpaw_ee.extensions import CloudIconsMcpProvider

        out = CloudIconsMcpProvider().build_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_icons"
            assert server is not None

    def test_build_server_returns_object(self) -> None:
        out = icons_mcp.build_icons_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_icons"
            assert server is not None

    def test_provider_is_ambient_not_opt_in(self) -> None:
        """The icons server must NOT be opt-in — otherwise the bundled site skill
        couldn't reach it without an explicit per-agent opt-in."""
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

        assert "pocketpaw_icons" not in OPT_IN_MCP_SERVERS


# ---------------------------------------------------------------------------
# Handler — search_icons
# ---------------------------------------------------------------------------


class TestSearchIconsHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_icons_with_https_svg_urls(self, restore_transport) -> None:
        """A successful search returns icons whose ``url`` is a real https SVG
        hotlink derived from the Iconify ``prefix:name`` id."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={"icons": ["mdi:calendar", "lucide:calendar-days", "tabler:calendar"]},
            )

        icons_mcp._TRANSPORT = _mock_transport(handler)

        out = await icons_mcp._search_handler({"query": "calendar", "limit": 5})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["ok"] is True
        assert body["count"] == 3
        first = body["icons"][0]
        assert first["id"] == "mdi:calendar"
        assert first["prefix"] == "mdi"
        assert first["name"] == "calendar"
        assert first["url"] == "https://api.iconify.design/mdi/calendar.svg"
        # every url is a real https SVG hotlink
        assert all(
            i["url"].startswith("https://") and i["url"].endswith(".svg") for i in body["icons"]
        )
        # the query reached the provider search endpoint
        assert "query=calendar" in captured["url"]

    @pytest.mark.asyncio
    async def test_style_hint_passed_through_as_filter(self, restore_transport) -> None:
        """An optional ``style`` hint is forwarded to the provider as a filter."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"icons": ["lucide:shield"]})

        icons_mcp._TRANSPORT = _mock_transport(handler)

        out = await icons_mcp._search_handler({"query": "shield", "style": "outline"})

        assert not out.get("is_error")
        assert "category=outline" in captured["url"]

    @pytest.mark.asyncio
    async def test_empty_query_returns_error_not_raise(self, restore_transport) -> None:
        """An empty/blank query returns an ``_error_response`` and never raises —
        and never touches the provider."""
        called = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            called["hit"] = True
            return httpx.Response(200, json={"icons": []})

        icons_mcp._TRANSPORT = _mock_transport(handler)

        out = await icons_mcp._search_handler({"query": "   "})

        assert out.get("is_error") is True
        assert "non-empty" in out["content"][0]["text"]
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_missing_query_returns_error(self, restore_transport) -> None:
        out = await icons_mcp._search_handler({})
        assert out.get("is_error") is True
        assert "query" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_provider_error_soft_fails_to_error_response(self, restore_transport) -> None:
        """A provider/httpx error is caught and surfaced as an is_error response,
        not raised into the agent."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream unavailable")

        icons_mcp._TRANSPORT = _mock_transport(handler)

        out = await icons_mcp._search_handler({"query": "calendar"})

        assert out.get("is_error") is True
        assert "icon search failed" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_transport_exception_soft_fails(self, restore_transport) -> None:
        """A raw transport exception (connection error) is also caught soft."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        icons_mcp._TRANSPORT = _mock_transport(handler)

        out = await icons_mcp._search_handler({"query": "calendar"})

        assert out.get("is_error") is True
        assert "icon search failed" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_limit_defaulted_and_capped(self, restore_transport) -> None:
        """A missing/invalid limit defaults to 12; an oversized one is capped
        at 60 before it reaches the provider."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"icons": []})

        icons_mcp._TRANSPORT = _mock_transport(handler)

        await icons_mcp._search_handler({"query": "calendar"})
        assert "limit=12" in captured["url"]

        await icons_mcp._search_handler({"query": "calendar", "limit": 999})
        assert "limit=60" in captured["url"]

    @pytest.mark.asyncio
    async def test_malformed_icon_ids_skipped(self, restore_transport) -> None:
        """Provider entries that aren't ``prefix:name`` strings are dropped rather
        than emitting a broken url."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"icons": ["mdi:calendar", "no-colon", 42]})

        icons_mcp._TRANSPORT = _mock_transport(handler)

        out = await icons_mcp._search_handler({"query": "calendar"})
        body = _decode_payload(out)
        assert body["count"] == 1
        assert body["icons"][0]["id"] == "mdi:calendar"
