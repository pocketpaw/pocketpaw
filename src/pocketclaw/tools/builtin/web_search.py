# Web Search tool — search the web via Tavily or Brave APIs.
# Created: 2026-02-06
# Part of Phase 1 Quick Wins

import logging
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.tools.protocol import BaseTool

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
        else:
            return self._error(
                f"Unknown search provider '{provider}'. Use 'tavily', 'brave', or 'parallel'."
            )

    async def _search_tavily(self, query: str, num_results: int, api_key: str | None) -> str:
        if not api_key:
            return self._error(
                "Tavily API key not configured. "
                "Set POCKETCLAW_TAVILY_API_KEY or switch to 'brave' provider."
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
                "Set POCKETCLAW_BRAVE_SEARCH_API_KEY or switch to 'tavily' provider."
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
                "Set POCKETCLAW_PARALLEL_API_KEY or switch to 'tavily'/'brave' provider."
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

    @staticmethod
    def _format_results(query: str, results: list[dict]) -> str:
        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            snippet = r.get("content", "")[:200]
            lines.append(f"{i}. **{title}**\n   {url}\n   {snippet}\n")
        return "\n".join(lines)
