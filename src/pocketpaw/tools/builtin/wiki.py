# Wikipedia info tool — topic summaries via Wikipedia's free REST API.
# Created: 2026-05-31 — zero-config builtin tool (no API key required).
#
# Calls https://en.wikipedia.org/api/rest_v1/page/summary/{title} which is
# free and keyless. Returns a clean text summary the agent renders; no inline
# ui-spec plumbing (the inline-primitive layer is owned by a separate RFC).

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
# Wikipedia asks REST clients to send a descriptive User-Agent.
_USER_AGENT = "PocketPaw/1.0 (https://github.com/pocketpaw/pocketpaw)"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]*>", "", text).strip()


class WikiTool(BaseTool):
    """Look up a topic on Wikipedia and return a concise summary.

    Uses Wikipedia's free REST summary API (no key). Good for "what is X",
    "who is X", or background facts on a person, place, or concept.
    """

    @property
    def name(self) -> str:
        return "wiki"

    @property
    def description(self) -> str:
        return (
            "Look up any topic on Wikipedia and get a concise summary. "
            "No API key required. Use for 'what is X', 'who is X', 'tell me about X', "
            "or background facts on a person, place, thing, or concept."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        'The topic to look up, e.g. "Eiffel Tower", "Alan Turing", '
                        '"photosynthesis".'
                    ),
                },
            },
            "required": ["topic"],
        }

    async def execute(self, topic: str) -> str:
        if not topic or not topic.strip():
            return self._error("No topic provided.")

        # Wikipedia title path uses underscores for spaces.
        title = quote(topic.strip().replace(" ", "_"), safe="")
        url = f"{_SUMMARY_URL}{title}"

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
        except httpx.TimeoutException:
            return self._error("Wikipedia request timed out. Please try again.")
        except Exception as e:
            return self._error(f"Wikipedia lookup failed: {e}")

        if resp.status_code == 404:
            return self._error(f"No Wikipedia article found for '{topic}'. Try a different term.")
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return self._error(f"Wikipedia API error: {e.response.status_code}")

        try:
            data = resp.json()
        except Exception:
            return self._error("Wikipedia returned an unreadable response.")

        extract = (data.get("extract") or "").strip()
        if not extract:
            return self._error(f"No summary available for '{topic}'. Try a different term.")

        return self._format(data, extract)

    @staticmethod
    def _format(data: dict, extract: str) -> str:
        title = _strip_html(data.get("displaytitle") or "") or data.get("title", "")
        lines = [title] if title else []

        description = data.get("description")
        if description:
            lines.append(f"({description})")

        lines.append("")
        lines.append(extract)

        coords = data.get("coordinates")
        if coords and "lat" in coords and "lon" in coords:
            lines.append(f"\nLocation: {coords['lat']:.4f}, {coords['lon']:.4f}")

        url = (data.get("content_urls") or {}).get("desktop", {}).get("page")
        if url:
            lines.append(f"Source: {url}")

        return "\n".join(lines)
