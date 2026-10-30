# tests/cloud/ai_visibility/test_engines.py — engine adapter parsing and calls.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). Recorded JSON fixtures built
# from each provider's documented response shape: text, cited URLs, consulted
# URLs and cost per adapter; request bodies carry the location; retries on 5xx;
# a provider error that echoes the key is scrubbed. No network: httpx.MockTransport.
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud.ai_visibility import engines
from pocketpaw_ee.cloud.ai_visibility.domain import Location
from pocketpaw_ee.cloud.ai_visibility.engines import (
    ClaudeWebSearch,
    EngineError,
    OpenAIWebSearch,
    PerplexityAgent,
    clean_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "engines"
AUSTIN = Location(city="Austin", country="US", region="Texas", timezone="America/Chicago")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_openai_parse() -> None:
    a = OpenAIWebSearch("sk-test").parse(_fixture("openai_web_search.json"))
    assert a.engine == "openai" and a.model == "gpt-6-luna"
    assert "Joe's Pizza near Zilker" in a.text
    # utm_source=openai stripped, deduped
    assert a.cited_urls == (
        "https://www.yelp.com/biz/home-slice-pizza-austin",
        "https://www.joespizzaatx.com/menu",
    )
    assert len(a.consulted_urls) == 4
    assert "https://www.reddit.com/r/Austin/comments/abc123/best_pizza/" in a.consulted_urls
    # 2100 * 0.10/1M + 180 * 0.50/1M + 1 non-reasoning search at $25/1K
    assert a.cost_usd == pytest.approx(0.00021 + 0.00009 + 0.025)
    assert a.raw_usage["web_search_calls"] == 1


def test_openai_reasoning_search_price() -> None:
    a = OpenAIWebSearch("k", reasoning_model=True).parse(_fixture("openai_web_search.json"))
    assert a.cost_usd == pytest.approx(0.0003 + 0.010)


def test_perplexity_parse_markers_and_reported_cost() -> None:
    a = PerplexityAgent("pplx-test").parse(_fixture("perplexity_agent.json"))
    assert a.engine == "perplexity" and a.model == "openai/gpt-6-luna"
    # [1], [web:3] and [2] are cited; result 4 (the GBP page) is consulted only
    assert a.cited_urls == (
        "https://www.homeslicepizza.com/",
        "https://www.via313.com/",
        "https://www.tripadvisor.com/Restaurants-g30196-c31-Austin_Texas.html",
    )
    assert len(a.consulted_urls) == 4
    assert a.cost_usd == pytest.approx(0.00266)  # provider-reported wins


def test_perplexity_cost_falls_back_to_table() -> None:
    data = _fixture("perplexity_agent.json")
    data["usage"].pop("cost")
    a = PerplexityAgent("k").parse(data)
    assert a.cost_usd == pytest.approx(0.0001 + 0.00006 + 0.0025)


def test_claude_parse() -> None:
    a = ClaudeWebSearch("sk-ant-test").parse(_fixture("claude_web_search.json"))
    assert a.engine == "claude" and a.model == "claude-haiku-4-5"
    assert a.text.startswith("I'll search") and "slow service" in a.text
    assert a.cited_urls == ("https://www.yelp.com/biz/joes-pizza-austin-2",)
    assert a.consulted_urls == (
        "https://www.yelp.com/biz/joes-pizza-austin-2",
        "https://www.eater.com/austin/best-pizza",
    )
    assert a.cost_usd == pytest.approx(0.006 + 0.0015 + 0.010)


def test_claude_search_error_block_is_not_a_source() -> None:
    data = _fixture("claude_web_search.json")
    data["content"][2]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "too_many_requests",
    }
    a = ClaudeWebSearch("k").parse(data)
    assert a.consulted_urls == ("https://www.yelp.com/biz/joes-pizza-austin-2",)


def test_request_bodies_carry_location() -> None:
    oa = OpenAIWebSearch("k").body("best pizza in Austin", AUSTIN)
    assert oa["tools"][0]["user_location"] == {
        "type": "approximate",
        "country": "US",
        "city": "Austin",
        "region": "Texas",
        "timezone": "America/Chicago",
    }
    assert oa["include"] == ["web_search_call.action.sources"]
    px = PerplexityAgent("k").body("q", Location(city="Austin", country="US", latitude=30.2))
    assert px["preset"] == "fast"
    assert px["tools"][0]["user_location"] == {"country": "US", "city": "Austin", "latitude": 30.2}
    cl = ClaudeWebSearch("k").body("q", AUSTIN)["tools"][0]
    assert cl["type"] == "web_search_20250305" and cl["user_location"]["city"] == "Austin"


async def test_ask_sends_auth_and_parses() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("x-api-key")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_fixture("claude_web_search.json"))

    eng = ClaudeWebSearch("sk-ant-test", transport=httpx.MockTransport(handler))
    a = await eng.ask("best pizza in Austin", AUSTIN)
    assert seen == {"auth": "sk-ant-test", "url": "https://api.anthropic.com/v1/messages"}
    assert a.cited_urls


async def test_retries_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(engines.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_fixture("openai_web_search.json"))

    a = await OpenAIWebSearch("k", transport=httpx.MockTransport(handler)).ask("q", AUSTIN)
    assert calls["n"] == 3 and a.text


async def test_error_scrubs_key_and_does_not_retry_4xx(monkeypatch) -> None:
    monkeypatch.setattr(engines.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="invalid key sk-secret-123")

    eng = PerplexityAgent("sk-secret-123", transport=httpx.MockTransport(handler))
    with pytest.raises(EngineError) as exc:
        await eng.ask("q", AUSTIN)
    assert "sk-secret-123" not in str(exc.value) and "***" in str(exc.value)
    assert calls["n"] == 1


def test_clean_url() -> None:
    assert clean_url("https://a.com/x?utm_source=openai&id=2#top") == "https://a.com/x?id=2"


def test_default_engines_only_configured(monkeypatch) -> None:
    from pocketpaw import config

    class _S:
        ai_visibility_openai_api_key = "sk-x"
        ai_visibility_perplexity_api_key = None
        ai_visibility_anthropic_api_key = " "

    monkeypatch.setattr(config, "get_settings", lambda: _S())
    assert [e.name for e in engines.default_engines()] == ["openai"]


async def _no_sleep(_s: float) -> None:
    return None
