# tests/test_refero_tool.py — the Refero design-research tool.
#
# Created 2026-09-15 (feat/refero-design-research).
#
# The load-bearing tests here are the DEGRADATION ones. Refero needs a paid
# plan, so "no token configured" is the common case in the wild, and a site
# build must proceed without design research rather than fail. Every public
# helper therefore has to swallow its own failures — and a test that only ever
# exercises the happy path would not notice if one of them started raising into
# a chat turn.
#
# The transport is an ``httpx.MockTransport`` injected at ``refero._TRANSPORT``,
# the same seam ``stock_images`` exposes, so the JSON-RPC exchange is exercised
# for real (headers, method names, session echo) with no network.

from __future__ import annotations

import json

import httpx
import pytest

from pocketpaw.tools.builtin import refero


@pytest.fixture
def token(monkeypatch):
    """Configure a Refero token for the duration of one test.

    ``get_settings`` is ``@lru_cache``d, and its ``force_reload`` flag is part of
    the cache key — so ``force_reload=True`` works exactly once per process and
    is useless here. ``cache_clear()`` is the only reliable reset.
    """
    from pocketpaw import config as pp_config

    monkeypatch.setenv("POCKETPAW_REFERO_API_TOKEN", "tok_test")
    pp_config.get_settings.cache_clear()
    yield
    monkeypatch.delenv("POCKETPAW_REFERO_API_TOKEN", raising=False)
    pp_config.get_settings.cache_clear()


def _mock(handler):
    """Install a MockTransport for one test and hand back the captured requests."""
    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    refero._TRANSPORT = httpx.MockTransport(_capture)
    return seen


@pytest.fixture(autouse=True)
def _clear_transport():
    yield
    refero._TRANSPORT = None


def _rpc_method(request: httpx.Request) -> str:
    return json.loads(request.content.decode())["method"]


def _json_server(tool_result, *, session: str | None = None):
    """A minimal streamable-HTTP MCP server answering with plain JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = _rpc_method(request)
        if method == "initialize":
            headers = {"Mcp-Session-Id": session} if session else {}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}},
                headers=headers,
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": json.dumps(tool_result)}]},
            },
        )

    return handler


# ── degradation: the common case in the wild ────────────────────────────────


def test_unconfigured_reports_itself_and_makes_no_call():
    """No token: every entry point returns empty, and nothing hits the network.

    ``_TRANSPORT`` is left as the autouse fixture set it (None), so a request
    escaping here would be a real connection attempt and the test would hang or
    error rather than quietly pass.
    """
    assert refero.is_configured() is False
    assert refero.search_styles("editorial SaaS") == []
    assert refero.get_style("abc") == {}
    assert refero.search_screens("pricing page") == []


def test_a_rejected_token_degrades_rather_than_raising(token):
    """401 is the shape a lapsed plan takes. It must not reach the chat turn."""
    _mock(lambda r: httpx.Response(401, json={"error": "Unauthorized"}))
    assert refero.search_styles("anything") == []
    assert refero.get_style("abc") == {}


def test_a_server_error_degrades(token):
    _mock(lambda r: httpx.Response(500, text="boom"))
    assert refero.search_styles("anything") == []


def test_a_jsonrpc_error_degrades(token):
    def handler(request):
        if _rpc_method(request) == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "nope"}}
        )

    _mock(handler)
    assert refero.search_styles("anything") == []


# ── the JSON-RPC exchange ───────────────────────────────────────────────────


def test_search_styles_sends_the_token_and_the_right_tool(token):
    seen = _mock(_json_server({"records": [{"uuid": "u1", "title": "Depot"}]}))

    results = refero.search_styles("developer tool website")

    assert results == [
        {
            "uuid": "u1",
            "title": "Depot",
            "url": None,
            "preview_url": None,
            "description": None,
        }
    ]
    # The handshake happened before the call, and the call named Refero's tool.
    assert [_rpc_method(r) for r in seen] == ["initialize", "tools/call"]
    call = json.loads(seen[-1].content.decode())
    assert call["params"]["name"] == "refero_search_styles"
    assert call["params"]["arguments"]["query"] == "developer tool website"
    # Without this the request is anonymous and Refero answers 401.
    assert seen[0].headers["authorization"] == "Bearer tok_test"


def test_a_session_id_is_echoed_back_on_the_call(token):
    """A server that opens a session rejects a tools/call that omits its id."""
    seen = _mock(_json_server({"records": []}, session="sess-42"))

    refero.search_styles("anything")

    methods = [_rpc_method(r) for r in seen]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert seen[-1].headers["mcp-session-id"] == "sess-42"


def test_an_event_stream_response_is_parsed(token):
    """Streamable HTTP may answer with SSE instead of JSON — both are legal."""

    def handler(request):
        if _rpc_method(request) == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"records": [{"uuid": "u9"}]})}]
                },
            }
        )
        return httpx.Response(
            200,
            text=f"event: message\ndata: {frame}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    _mock(handler)
    assert [r["uuid"] for r in refero.search_styles("x")] == ["u9"]


def test_structured_content_wins_over_text(token):
    """A server that returns structuredContent should not be re-parsed from prose."""

    def handler(request):
        if _rpc_method(request) == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "structuredContent": {"records": [{"uuid": "structured"}]},
                    "content": [{"type": "text", "text": "human prose, not json"}],
                },
            },
        )

    _mock(handler)
    assert [r["uuid"] for r in refero.search_styles("x")] == ["structured"]


def test_get_style_returns_the_system_verbatim(token):
    """The ROLES are the value — reshaping to a fixed schema is how they get lost."""
    full = {
        "northStar": "restrained editorial",
        "colors": [{"hex": "#111", "role": "cta-only"}],
        "donts": ["no gradients"],
    }
    _mock(_json_server(full))

    assert refero.get_style("uuid-1") == full


def test_search_screens_flattens_the_source_and_coerces_platform(token):
    seen = _mock(
        _json_server(
            {
                "records": [
                    {
                        "uuid": "s1",
                        "site": {"name": "Luma", "domain": "lu.ma"},
                        "content": {"description": "a pricing page"},
                        "ui_elements": ["Table"],
                    }
                ]
            }
        )
    )

    rows = refero.search_screens("pricing page", platform="android")

    assert rows[0]["source"] == "Luma"
    assert rows[0]["description"] == "a pricing page"
    assert rows[0]["ui_elements"] == ["Table"]
    # Refero only knows web and ios; a Paw Site can only be web.
    assert json.loads(seen[-1].content.decode())["params"]["arguments"]["platform"] == "web"


def test_a_search_result_is_capped(token):
    _mock(_json_server({"records": [{"uuid": f"u{i}"} for i in range(50)]}))
    assert len(refero.search_styles("x", limit=3)) == 3


# ── the BaseTool surface (the non-SDK backends) ─────────────────────────────


@pytest.mark.asyncio
async def test_the_styles_tool_searches_and_returns_json(token):
    _mock(_json_server({"records": [{"uuid": "u1", "title": "Depot"}]}))

    out = json.loads(await refero.ReferoStylesTool().execute(query="editorial SaaS"))

    assert out["ok"] is True
    assert out["results"][0]["title"] == "Depot"


@pytest.mark.asyncio
async def test_the_styles_tool_fetches_one_style_when_given_an_id(token):
    seen = _mock(_json_server({"northStar": "x"}))

    out = json.loads(await refero.ReferoStylesTool().execute(style_id="uuid-1"))

    assert out["style"] == {"northStar": "x"}
    assert json.loads(seen[-1].content.decode())["params"]["name"] == "refero_get_style"


@pytest.mark.asyncio
async def test_the_tools_refuse_an_empty_query_without_calling_out():
    """A blank query must not become a wasted upstream call against the quota."""
    styles = json.loads(await refero.ReferoStylesTool().execute())
    screens = json.loads(await refero.ReferoScreensTool().execute(query="  "))
    assert styles["ok"] is False
    assert screens["ok"] is False


def test_both_tools_declare_a_usable_schema():
    """A tool whose schema omits its own argument is unreachable in practice."""
    for tool in (refero.ReferoStylesTool(), refero.ReferoScreensTool()):
        assert tool.name.startswith("refero_")
        assert "query" in tool.parameters["properties"]
        assert tool.description.strip()
