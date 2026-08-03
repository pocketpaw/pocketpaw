# Web Search tool — search the web via Tavily, Brave, Parallel, or the
# LiteLLM proxy's Search API.
# Created: 2026-02-06
# Part of Phase 1 Quick Wins
# 2026-08-01: added the 'litellm' provider. It POSTs to the proxy's
#   /v1/search with a `search_tool_name`, so a deployment that already talks
#   to a LiteLLM gateway gets web search with no second vendor key and no
#   second egress path. Whatever the operator registered there (parallel_ai,
#   tinyfish, …) is reachable by name.

import logging
from typing import Any

import httpx

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1beta/search"


class WebSearchTool(BaseTool):
    """Search the web using Tavily, Brave, or Parallel AI Search API."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for current information. Returns a list of results "
            "with titles, URLs, and snippets. Useful for answering questions "
            "about recent events, looking up documentation, or finding resources."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, num_results: int = 5) -> str:
        """Execute a web search."""
        settings = get_settings()
        num_results = min(max(num_results, 1), 10)

        provider = settings.web_search_provider

        if provider == "tavily":
            return await self._search_tavily(query, num_results, settings.tavily_api_key)
        elif provider == "brave":
            return await self._search_brave(query, num_results, settings.brave_search_api_key)
        elif provider == "parallel":
            return await self._search_parallel(query, num_results, settings.parallel_api_key)
        elif provider == "litellm":
            return await self._search_litellm(query, num_results, settings)
        else:
            return self._error(
                f"Unknown search provider '{provider}'. "
                "Use 'tavily', 'brave', 'parallel', or 'litellm'."
            )

    async def _search_tavily(self, query: str, num_results: int, api_key: str | None) -> str:
        if not api_key:
            return self._error(
                "Tavily API key not configured. "
                "Set POCKETPAW_TAVILY_API_KEY or switch to 'brave' provider."
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    _TAVILY_URL,
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": num_results,
                        "include_answer": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No results found for: {query}"

            return self._format_results(query, results[:num_results])

        except httpx.HTTPStatusError as e:
            return self._error(f"Tavily API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Search failed: {e}")

    async def _search_brave(self, query: str, num_results: int, api_key: str | None) -> str:
        if not api_key:
            return self._error(
                "Brave Search API key not configured. "
                "Set POCKETPAW_BRAVE_SEARCH_API_KEY or switch to 'tavily' provider."
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    _BRAVE_URL,
                    params={"q": query, "count": num_results},
                    headers={
                        "X-Subscription-Token": api_key,
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            web_results = data.get("web", {}).get("results", [])
            if not web_results:
                return f"No results found for: {query}"

            # Normalize Brave results to common format
            results = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("description", ""),
                }
                for r in web_results[:num_results]
            ]
            return self._format_results(query, results)

        except httpx.HTTPStatusError as e:
            return self._error(f"Brave API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Search failed: {e}")

    async def _search_parallel(self, query: str, num_results: int, api_key: str | None) -> str:
        if not api_key:
            return self._error(
                "Parallel AI API key not configured. "
                "Set POCKETPAW_PARALLEL_API_KEY or switch to 'tavily'/'brave' provider."
            )

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    _PARALLEL_SEARCH_URL,
                    headers={
                        "x-api-key": api_key,
                        "parallel-beta": "search-extract-2025-10-10",
                        "Content-Type": "application/json",
                    },
                    json={
                        "search_queries": [query],
                        "max_results": num_results,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No results found for: {query}"

            # Normalize Parallel results to common format
            normalized = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": " ".join(r.get("excerpts", [])),
                }
                for r in results[:num_results]
            ]
            return self._format_results(query, normalized)

        except httpx.HTTPStatusError as e:
            return self._error(f"Parallel AI API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Search failed: {e}")

    async def _search_litellm(self, query: str, num_results: int, settings: Any) -> str:
        """Search through the LiteLLM proxy's Search API rather than a vendor.

        Worth being precise about why this is a TOOL provider and not one of
        the pydantic_ai backend's native web capabilities, because the native
        route looks like it should work and does not. pydantic-ai's
        ``WebSearch`` asks the model's own provider to run the search inside
        ``chat/completions``. Sending that at this gateway returns 200 and
        searches NOTHING — measured 2026-08-01, both ``web_search_options={}``
        and ``tools=[{"type": "web_search"}]`` came back with no citations and
        a model that still said it had no live access. The gateway's search
        lives behind a SEPARATE endpoint, so a tool is what can reach it.

        Going through the proxy rather than straight to the vendor is what
        makes this worth having next to ``_search_parallel``: no second key to
        distribute, no second egress path to allow, and the proxy keeps the
        usage accounting for search alongside completions.
        """
        # ``litellm_search_api_base`` wins when set. It exists for the case
        # where something is chained in FRONT of the gateway: a compression or
        # observability proxy intercepts /v1/chat/completions and friends and
        # knows nothing about /v1/search, so a deployment that repoints
        # ``litellm_api_base`` at it would 404 every search while completions
        # kept working — a break that shows up only in the tool.
        base = str(
            getattr(settings, "litellm_search_api_base", None)
            or getattr(settings, "litellm_api_base", "")
            or ""
        ).rstrip("/")
        key = getattr(settings, "litellm_api_key", None)
        if not base:
            return self._error(
                "LiteLLM base URL not configured. Set POCKETPAW_LITELLM_API_BASE "
                "(or POCKETPAW_LITELLM_SEARCH_API_BASE) or switch to 'tavily'/'brave'."
            )
        tool_name = str(getattr(settings, "litellm_search_tool_name", "") or "web_search")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{base}/v1/search",
                    headers={
                        # The proxy accepts an unauthenticated call only if it
                        # was configured that way; sending an empty bearer is
                        # worse than sending none.
                        **({"Authorization": f"Bearer {key}"} if key else {}),
                        "Content-Type": "application/json",
                    },
                    json={
                        "search_tool_name": tool_name,
                        "query": query,
                        "max_results": num_results,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if not results:
                return f"No results found for: {query}"

            # The gateway returns ``snippet``; ``_format_results`` reads
            # ``content``. Normalising here keeps one output shape across all
            # four providers rather than teaching the formatter a second one.
            normalized = [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("snippet") or r.get("content") or "",
                }
                for r in results[:num_results]
            ]
            return self._format_results(query, normalized)

        except httpx.HTTPStatusError as e:
            # A wrong ``search_tool_name`` is the likely misconfiguration and
            # the proxy names it in the body, so pass that through instead of
            # reporting a bare status code.
            detail = ""
            try:
                detail = f" — {e.response.json().get('error', {}).get('message', '')}"
            except Exception:  # noqa: BLE001
                detail = ""
            return self._error(
                f"LiteLLM search API error: {e.response.status_code}{detail}. "
                f"Registered tools: GET {base}/v1/search/tools"
            )
        except Exception as e:
            return self._error(f"Search failed: {e}")

    @staticmethod
    def _format_results(query: str, results: list[dict]) -> str:
        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            snippet = r.get("content", "")[:200]
            lines.append(f"{i}. **{title}**\n   {url}\n   {snippet}\n")
        return "\n".join(lines)
