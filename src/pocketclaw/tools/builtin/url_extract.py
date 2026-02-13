# URL Extract tool — fetch clean content from URLs via Parallel AI or local fallback.
# Created: 2026-02-06

import logging
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_PARALLEL_EXTRACT_URL = "https://api.parallel.ai/v1beta/extract"
_MAX_CONTENT_CHARS = 50_000


class UrlExtractTool(BaseTool):
    """Extract clean text content from one or more URLs."""

    @property
    def name(self) -> str:
        return "url_extract"

    @property
    def description(self) -> str:
        return (
            "Fetch and extract clean text content from one or more URLs. "
            "Useful for reading web pages, articles, documentation, or any "
            "publicly accessible URL. Returns markdown-formatted content."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs to extract content from",
                },
            },
            "required": ["urls"],
        }

    async def execute(self, urls: list[str]) -> str:
        """Extract content from the given URLs."""
        if not urls:
            return self._error("No URLs provided.")

        settings = get_settings()
        provider = settings.url_extract_provider

        if provider == "auto":
            if settings.parallel_api_key:
                provider = "parallel"
            else:
                provider = "local"

        if provider == "parallel":
            return await self._extract_parallel(urls, settings.parallel_api_key)
        elif provider == "local":
            return await self._extract_local(urls)
        else:
            return self._error(
                f"Unknown extract provider '{provider}'. Use 'auto', 'parallel', or 'local'."
            )

    async def _extract_parallel(self, urls: list[str], api_key: str | None) -> str:
        if not api_key:
            return self._error(
                "Parallel AI API key not configured. "
                "Set POCKETCLAW_PARALLEL_API_KEY or switch to 'local' provider."
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    _PARALLEL_EXTRACT_URL,
                    headers={
                        "x-api-key": api_key,
                        "parallel-beta": "search-extract-2025-10-10",
                        "Content-Type": "application/json",
                    },
                    json={
                        "urls": urls,
                        "full_content": True,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            errors = data.get("errors", [])

            if not results and errors:
                error_msgs = "; ".join(
                    f"{e.get('url', '?')}: {e.get('error', 'unknown')}" for e in errors
                )
                return self._error(f"Extraction failed: {error_msgs}")

            if not results:
                return self._error("No content extracted from the provided URLs.")

            return self._format_results(results, urls)

        except httpx.HTTPStatusError as e:
            return self._error(f"Parallel AI API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Extraction failed: {e}")

    async def _extract_local(self, urls: list[str]) -> str:
        try:
            import html2text
        except ImportError:
            return self._error(
                "html2text not installed. Install with: pip install 'pocketpaw[extract]'"
            )

        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0

        results = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()

                    content_type = resp.headers.get("content-type", "")
                    if "text/html" in content_type:
                        text = converter.handle(resp.text)
                    else:
                        text = resp.text

                    results.append(
                        {
                            "url": url,
                            "title": _extract_title(resp.text)
                            if "text/html" in content_type
                            else url,
                            "full_content": text[:_MAX_CONTENT_CHARS],
                        }
                    )
                except Exception as e:
                    results.append(
                        {
                            "url": url,
                            "title": url,
                            "full_content": f"Error fetching URL: {e}",
                        }
                    )

        if not results:
            return self._error("No content extracted from the provided URLs.")

        return self._format_results(results, urls)

    @staticmethod
    def _format_results(results: list[dict], urls: list[str]) -> str:
        if len(urls) == 1:
            r = results[0]
            title = r.get("title", "Untitled")
            content = r.get("full_content", "")[:_MAX_CONTENT_CHARS]
            return f"# {title}\n\n{content}"

        # Multiple URLs: numbered list with previews
        lines = [f"Extracted content from {len(results)} URLs:\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            content = r.get("full_content", "")[:2000]
            lines.append(f"## {i}. {title}\n{url}\n\n{content}\n")
        return "\n".join(lines)


def _extract_title(html: str) -> str:
    """Extract <title> from HTML, falling back to 'Untitled'."""
    import re

    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return "Untitled"
