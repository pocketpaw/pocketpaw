# AI visibility — engines: ask an AI search engine one local question.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). An ``Engine`` answers
# ``ask(question, location) -> EngineAnswer`` with the answer text, the URLs it
# cites, every URL it consulted (when the API exposes them) and the call's cost.
# Three adapters, all plain httpx (no SDKs): ``OpenAIWebSearch``,
# ``PerplexityAgent``, ``ClaudeWebSearch``. Only engines whose terms allow storing
# and analysing results are here: never Gemini "Grounding with Google Search" or
# Bing grounding, never consumer-app scraping.
#
# Keys come from Settings (``ai_visibility_*``, secrets, see ``default_engines``).
# A key is only ever placed in a request header; provider error text is scrubbed
# of it (``_scrub``) before it is raised, because a 401 body can echo it.
#
# Spike findings, checked against the provider docs on 2026-10-03 (no keys on the
# dev box, so every adapter's live run is PENDING KEYS; fixtures are built from
# the documented shapes):
#   * OpenAI Responses API: tool ``{"type": "web_search", "user_location":
#     {"type": "approximate", country, city, region}}`` (``timezone`` is not on the
#     docs page; we send it, unconfirmed). ``include: ["web_search_call.action.
#     sources"]`` puts every consulted URL on ``web_search_call.action.sources``;
#     answer citations are ``url_citation`` annotations on ``output_text``.
#     gpt-6-luna is $0.10 / $0.50 per 1M and is NOT a reasoning model, so web
#     search bills at the non-reasoning rate: $25 / 1K calls, search content
#     tokens free (reasoning models: $10 / 1K + content tokens).
#   * Perplexity Agent API: ``POST /v1/agent`` (``/v1/responses`` is an alias we
#     avoid). ``preset: "fast"`` = one step, at most one tool call. Its model
#     differs between pages: presets page says ``openai/gpt-6-luna``, pricing page
#     says ``openai/gpt-5.6-luna``. ``user_location`` (country, region, city,
#     latitude, longitude) sits on the ``web_search`` TOOL object, not top level;
#     whether ``tools`` may be sent alongside ``preset`` is undocumented (pending
#     live check). Results arrive as an ``output[]`` item ``{"type":
#     "search_results", "results": [{id, url, title, snippet, date}]}``; inline
#     markers are ``[N]`` (the brief said ``[web:N]``; both are parsed). Web search
#     is $2.50 / 1K, or $1.00 / 1K with ``search_type: "fast"`` (alternatives, not
#     additive). ``usage.cost.total_cost`` is reported and preferred over our own
#     table; whether it includes the search fee is pending live check.
#   * Anthropic Messages API: ``web_search_20250305`` with ``user_location``
#     ``{type: approximate, city, region, country, timezone}``. Newer versions
#     (20260209+) default to code-execution callers and 400 on models without
#     programmatic tool calling, so we stay on 20250305. Citations are
#     ``web_search_result_location`` (url, title, cited_text); consulted URLs are
#     the ``web_search_tool_result`` blocks; ``usage.server_tool_use.
#     web_search_requests`` counts searches. $10 / 1K searches. Haiku 4.5 is $1 /
#     $5 per 1M; the docs do not list per-model support for web search, so Haiku
#     support is CONFIRM ON FIRST LIVE RUN (model is a constructor arg; Sonnet 5 at
#     $2 / $10 is the fallback).

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from pocketpaw_ee.cloud.ai_visibility.domain import EngineAnswer, Location

logger = logging.getLogger(__name__)

# Prices in USD, from provider docs checked 2026-10-03. Verify before billing on them.
# Token prices are per 1M tokens (input, output).
TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10, 0.50),
    "openai/gpt-6-luna": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "@cf/cloudflare/clef-flash": (0.09, 0.0),
}
# Per search / tool call.
SEARCH_PRICES: dict[str, float] = {
    "openai_non_reasoning": 25.00 / 1000,
    "openai_reasoning": 10.00 / 1000,
    "perplexity_web": 2.50 / 1000,
    "perplexity_fast": 1.00 / 1000,
    "anthropic": 10.00 / 1000,
}

TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class EngineError(Exception):
    """A provider call failed. The message never contains the API key."""


class Engine(Protocol):
    name: str

    async def ask(self, question: str, location: Location) -> EngineAnswer: ...


def token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = TOKEN_PRICES.get(model, (0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


async def post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    secret: str,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = TIMEOUT_SECONDS,
    backoff: float = 0.5,
) -> dict[str, Any]:
    """POST JSON with retries on transport errors, 429 and 5xx. Raises
    ``EngineError`` (key scrubbed) on a final failure."""
    last = ""
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.TransportError as exc:
                last = f"transport error: {type(exc).__name__}"
            else:
                if resp.status_code < 300:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in _RETRY_STATUSES:
                    break
            if attempt < MAX_RETRIES:
                await asyncio.sleep(backoff * 2**attempt)
    raise EngineError(_scrub(f"{urlsplit(url).netloc}: {last}", secret))


def clean_url(url: str) -> str:
    """Drop ``utm_*`` tracking params (OpenAI appends ``utm_source=openai``) and a
    trailing fragment so the same page dedupes across engines."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _dedupe(urls: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(clean_url(u) for u in urls if u))


def _approx_location(location: Location) -> dict[str, Any]:
    loc = {
        "type": "approximate",
        "country": location.country,
        "city": location.city,
        "region": location.region,
        "timezone": location.timezone,
    }
    return {k: v for k, v in loc.items() if v}


class OpenAIWebSearch:
    """OpenAI Responses API with the ``web_search`` tool."""

    name = "openai"
    url = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-6-luna",
        *,
        reasoning_model: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self.model = model
        self._search_price = SEARCH_PRICES[
            "openai_reasoning" if reasoning_model else "openai_non_reasoning"
        ]
        self._transport = transport

    def body(self, question: str, location: Location) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": question,
            "tools": [{"type": "web_search", "user_location": _approx_location(location)}],
            "include": ["web_search_call.action.sources"],
        }

    async def ask(self, question: str, location: Location) -> EngineAnswer:
        data = await post_json(
            self.url,
            {"Authorization": f"Bearer {self._key}"},
            self.body(question, location),
            secret=self._key,
            transport=self._transport,
        )
        return self.parse(data)

    def parse(self, data: dict[str, Any]) -> EngineAnswer:
        texts: list[str] = []
        cited: list[str] = []
        consulted: list[str] = []
        searches = 0
        for item in data.get("output") or []:
            if item.get("type") == "web_search_call":
                searches += 1
                action = item.get("action") or {}
                consulted += [s.get("url", "") for s in action.get("sources") or []]
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") != "output_text":
                        continue
                    texts.append(part.get("text", ""))
                    cited += [
                        a.get("url", "")
                        for a in part.get("annotations") or []
                        if a.get("type") == "url_citation"
                    ]
        usage = data.get("usage") or {}
        cost = (
            token_cost(self.model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            + searches * self._search_price
        )
        return EngineAnswer(
            engine=self.name,
            model=data.get("model") or self.model,
            text="\n".join(texts),
            cited_urls=_dedupe(cited),
            consulted_urls=_dedupe(consulted + cited),
            raw_usage={**usage, "web_search_calls": searches},
            cost_usd=cost,
        )


_PPLX_MARKER = re.compile(r"\[(?:web:)?(\d+)\]")


class PerplexityAgent:
    """Perplexity Agent API, ``fast`` preset, web search localised by the
    ``user_location`` on the tool."""

    name = "perplexity"
    url = "https://api.perplexity.ai/v1/agent"

    def __init__(
        self,
        api_key: str,
        preset: str = "fast",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self.preset = preset
        self._transport = transport

    def body(self, question: str, location: Location) -> dict[str, Any]:
        loc = {
            "country": location.country,
            "region": location.region,
            "city": location.city,
            "latitude": location.latitude,
            "longitude": location.longitude,
        }
        tool = {"type": "web_search", "user_location": {k: v for k, v in loc.items() if v}}
        return {"preset": self.preset, "input": question, "tools": [tool]}

    async def ask(self, question: str, location: Location) -> EngineAnswer:
        data = await post_json(
            self.url,
            {"Authorization": f"Bearer {self._key}"},
            self.body(question, location),
            secret=self._key,
            transport=self._transport,
        )
        return self.parse(data)

    def parse(self, data: dict[str, Any]) -> EngineAnswer:
        by_id: dict[int, str] = {}
        consulted: list[str] = []
        texts: list[str] = []
        searches = 0
        for item in data.get("output") or []:
            kind = item.get("type")
            if kind == "search_results":
                searches += 1
                for r in item.get("results") or []:
                    consulted.append(r.get("url", ""))
                    if isinstance(r.get("id"), int):
                        by_id[r["id"]] = r.get("url", "")
            elif kind == "fetch_url_results":
                consulted += [r.get("url", "") for r in item.get("results") or []]
            elif kind == "message":
                texts += [
                    p.get("text", "")
                    for p in item.get("content") or []
                    if p.get("type") == "output_text"
                ]
        text = data.get("output_text") or "\n".join(texts)
        cited = [by_id[int(n)] for n in _PPLX_MARKER.findall(text) if int(n) in by_id]
        usage = data.get("usage") or {}
        model = data.get("model") or "openai/gpt-6-luna"
        reported = (usage.get("cost") or {}).get("total_cost")
        if isinstance(reported, (int, float)) and reported > 0:
            cost = float(reported)
        else:
            cost = (
                token_cost(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
                + searches * SEARCH_PRICES["perplexity_web"]
            )
        return EngineAnswer(
            engine=self.name,
            model=model,
            text=text,
            cited_urls=_dedupe(cited),
            consulted_urls=_dedupe(consulted),
            raw_usage={**usage, "searches": searches},
            cost_usd=cost,
        )


class ClaudeWebSearch:
    """Anthropic Messages API with the basic ``web_search_20250305`` tool."""

    name = "claude"
    url = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        *,
        max_uses: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self.model = model
        self.max_uses = max_uses
        self._transport = transport

    def body(self, question: str, location: Location) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": question}],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": self.max_uses,
                    "user_location": _approx_location(location),
                }
            ],
        }

    async def ask(self, question: str, location: Location) -> EngineAnswer:
        data = await post_json(
            self.url,
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            self.body(question, location),
            secret=self._key,
            transport=self._transport,
        )
        return self.parse(data)

    def parse(self, data: dict[str, Any]) -> EngineAnswer:
        texts: list[str] = []
        cited: list[str] = []
        consulted: list[str] = []
        for block in data.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                texts.append(block.get("text", ""))
                cited += [
                    c.get("url", "")
                    for c in block.get("citations") or []
                    if c.get("type") == "web_search_result_location"
                ]
            elif kind == "web_search_tool_result" and isinstance(block.get("content"), list):
                consulted += [r.get("url", "") for r in block["content"]]
        usage = data.get("usage") or {}
        searches = (usage.get("server_tool_use") or {}).get("web_search_requests", 0)
        model = data.get("model") or self.model
        cost = (
            token_cost(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            + searches * SEARCH_PRICES["anthropic"]
        )
        return EngineAnswer(
            engine=self.name,
            model=model,
            text="".join(texts),
            cited_urls=_dedupe(cited),
            consulted_urls=_dedupe(consulted + cited),
            raw_usage=usage,
            cost_usd=cost,
        )


def default_engines() -> list[Engine]:
    """Every engine whose platform key is set in Settings."""
    from pocketpaw.config import get_settings

    settings = get_settings()

    def _get(name: str) -> str:
        return str(getattr(settings, name, None) or "").strip()

    engines: list[Engine] = []
    if key := _get("ai_visibility_openai_api_key"):
        engines.append(OpenAIWebSearch(key))
    if key := _get("ai_visibility_perplexity_api_key"):
        engines.append(PerplexityAgent(key))
    if key := _get("ai_visibility_anthropic_api_key"):
        engines.append(ClaudeWebSearch(key))
    return engines


__all__ = [
    "SEARCH_PRICES",
    "TOKEN_PRICES",
    "ClaudeWebSearch",
    "Engine",
    "EngineError",
    "OpenAIWebSearch",
    "PerplexityAgent",
    "clean_url",
    "default_engines",
    "post_json",
    "token_cost",
]
