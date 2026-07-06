# Stock image search tool — search free royalty-free stock photography from
# Pexels + Unsplash and return hotlink-ready CDN URLs.
#
# Created: 2026-07-04 (feat/paw-sites-stock-imagery). Closes the "generated Paw
# Sites ship text-and-color only" gap: the Svelte-track site-authoring agent can
# now source real photography and bake the returned CDN `url` straight into its
# hand-written sections. Provider logic ports joelio/stocky (MIT) but drops its
# MCP packaging — the shared `search_stock_images()` helper below is the single
# code path, surfaced two ways (this BaseTool for the non-SDK backends + an EE
# in-process MCP server for the claude_agent_sdk backend that runs the site
# skill), mirroring how image_gen shares `generate_image_file()`.
#
# v1 is HOTLINK-only: we return the provider CDN URL for direct embed in the
# static edge build (no R2 rehost yet — that's the named follow-up). Both
# providers permit hotlinking; Unsplash additionally requires firing the photo's
# `download_location` trigger on use, which we do best-effort on search so a
# hotlinked Unsplash photo stays inside Unsplash's API terms. Graceful
# degradation: zero configured keys → `[]` (site ships text-only, no regression);
# one provider errors/rate-limits → its results are simply absent.

from __future__ import annotations

import logging
from typing import Any

import httpx

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_PEXELS_ENDPOINT = "https://api.pexels.com/v1/search"
_UNSPLASH_ENDPOINT = "https://api.unsplash.com/search/photos"

# Marketing pages want a landscape hero + section imagery by default. Map our
# small orientation vocabulary onto each provider's own parameter values.
_PEXELS_ORIENTATION = {"landscape": "landscape", "portrait": "portrait", "square": "square"}
_UNSPLASH_ORIENTATION = {"landscape": "landscape", "portrait": "portrait", "square": "squarish"}

# Tests inject an httpx.MockTransport here so the provider HTTP calls are
# exercised without live network (same seam media.py exposes). Production leaves
# it None (real network).
_TRANSPORT: httpx.BaseTransport | None = None

# Bound each provider call so a slow/hanging API can't stall a site build.
_TIMEOUT_SECONDS = 10.0


def _client() -> httpx.Client:
    """A short-lived httpx client pointed at the real network (or the injected
    mock transport in tests)."""
    return httpx.Client(transport=_TRANSPORT, timeout=_TIMEOUT_SECONDS)


def _search_pexels(api_key: str, query: str, orientation: str, count: int) -> list[dict[str, Any]]:
    """Query Pexels and normalize results. Returns `[]` on any error — a provider
    outage must never fail the caller's build. `url` is the `large` rendition
    (marketing-suitable, ~940px wide, CDN-hosted)."""
    params = {
        "query": query,
        "per_page": count,
        "orientation": _PEXELS_ORIENTATION.get(orientation, "landscape"),
    }
    try:
        with _client() as client:
            resp = client.get(_PEXELS_ENDPOINT, params=params, headers={"Authorization": api_key})
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
    except Exception:  # noqa: BLE001 — degrade to no Pexels results, never raise
        logger.warning("stock_images: Pexels search failed for %r", query, exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for p in photos:
        src = p.get("src") or {}
        url = src.get("large") or src.get("original")
        if not url:
            continue
        photographer = p.get("photographer") or "Pexels"
        out.append(
            {
                "url": url,
                "alt": (p.get("alt") or query).strip(),
                "credit": f"Photo by {photographer} on Pexels",
                "credit_url": p.get("url", "https://www.pexels.com"),
                "provider": "pexels",
                "width": p.get("width"),
                "height": p.get("height"),
            }
        )
    return out


def _trigger_unsplash_download(client: httpx.Client, download_location: str, api_key: str) -> None:
    """Fire Unsplash's `links.download_location` trigger. Unsplash's API terms
    require this whenever a photo is 'used' (a hotlink embed counts). Best-effort:
    a failed trigger must never break search — the photo URL is still returned."""
    try:
        client.get(download_location, headers={"Authorization": f"Client-ID {api_key}"})
    except Exception:  # noqa: BLE001
        logger.debug("stock_images: Unsplash download-trigger failed", exc_info=True)


def _search_unsplash(
    api_key: str, query: str, orientation: str, count: int
) -> list[dict[str, Any]]:
    """Query Unsplash, normalize results, and fire each photo's download trigger
    (an Unsplash API-terms requirement on use). Returns `[]` on any error. `url`
    is the `regular` rendition (~1080px wide, CDN-hosted)."""
    params = {
        "query": query,
        "per_page": count,
        "orientation": _UNSPLASH_ORIENTATION.get(orientation, "landscape"),
    }
    try:
        with _client() as client:
            resp = client.get(
                _UNSPLASH_ENDPOINT,
                params=params,
                headers={"Authorization": f"Client-ID {api_key}"},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])

            out: list[dict[str, Any]] = []
            for p in results:
                url = (p.get("urls") or {}).get("regular")
                if not url:
                    continue
                user = p.get("user") or {}
                name = user.get("name") or "Unsplash"
                links = p.get("links") or {}
                download_location = links.get("download_location")
                if download_location:
                    _trigger_unsplash_download(client, download_location, api_key)
                out.append(
                    {
                        "url": url,
                        "alt": (p.get("alt_description") or p.get("description") or query).strip(),
                        "credit": f"Photo by {name} on Unsplash",
                        "credit_url": (user.get("links") or {}).get("html", "https://unsplash.com"),
                        "provider": "unsplash",
                        "width": p.get("width"),
                        "height": p.get("height"),
                    }
                )
            return out
    except Exception:  # noqa: BLE001 — degrade to no Unsplash results, never raise
        logger.warning("stock_images: Unsplash search failed for %r", query, exc_info=True)
        return []


def search_stock_images(
    query: str,
    orientation: str = "landscape",
    count: int = 5,
) -> list[dict[str, Any]]:
    """Search free stock photography across the configured providers.

    Returns up to ``count`` results, each a dict:
    ``{url, alt, credit, credit_url, provider, width, height}`` — where ``url``
    is a provider CDN hotlink at a marketing-suitable rendition, ready to embed
    directly in a static page.

    Providers are used when their key is configured (``POCKETPAW_PEXELS_API_KEY``,
    ``POCKETPAW_UNSPLASH_ACCESS_KEY``); with neither key set the function returns
    ``[]`` so callers degrade to imageless without a hard error. A single
    provider erroring or rate-limiting simply omits its results.
    """
    query = (query or "").strip()
    if not query:
        return []
    orientation = orientation if orientation in _PEXELS_ORIENTATION else "landscape"
    count = max(1, min(int(count) if isinstance(count, int) else 5, 30))

    settings = get_settings()
    results: list[dict[str, Any]] = []

    # Split the requested count across configured providers so a caller asking
    # for N gets ~N back rather than N-per-provider. Pexels leads when both are
    # present (permissive license, higher rate limit); Unsplash fills the rest.
    pexels_key = getattr(settings, "pexels_api_key", None)
    unsplash_key = getattr(settings, "unsplash_access_key", None)
    active = [k for k in (pexels_key, unsplash_key) if k]
    if not active:
        return []
    per_provider = max(1, count // len(active))

    if pexels_key:
        results.extend(_search_pexels(pexels_key, query, orientation, per_provider))
    if unsplash_key:
        results.extend(_search_unsplash(unsplash_key, query, orientation, per_provider))

    return results[:count]


class StockImageTool(BaseTool):
    """Search free stock photography (Pexels + Unsplash) and return hotlink URLs.

    Surfaced to the non-SDK agent backends; the claude_agent_sdk backend reaches
    the same `search_stock_images()` helper through the EE in-process MCP server.
    """

    @property
    def name(self) -> str:
        return "search_stock_images"

    @property
    def description(self) -> str:
        return (
            "Search free royalty-free stock photos (Pexels + Unsplash) for a "
            "query and return ready-to-embed image URLs. Use when building a "
            "page or site that needs real photography. Returns a list of "
            "{url, alt, credit, provider}. Embed `url` directly and show the "
            "`credit` line near the image. Returns an empty list when no photo "
            "provider is configured — proceed without imagery in that case."
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
                    "description": (
                        "What the photo should show. Prefer generic, descriptive "
                        "subjects ('modern dental office', 'artisan bakery bread') "
                        "over hyper-specific ones ('dentist in Akron')."
                    ),
                },
                "orientation": {
                    "type": "string",
                    "description": "Shape hint: 'landscape' (default), 'portrait', or 'square'.",
                    "default": "landscape",
                },
                "count": {
                    "type": "integer",
                    "description": "How many photos to return (default 5, max 30).",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        orientation: str = "landscape",
        count: int = 5,
    ) -> str:
        """Search stock providers and return the normalized results as text."""
        import json

        try:
            results = search_stock_images(query, orientation=orientation, count=count)
        except Exception as e:  # noqa: BLE001
            return self._error(f"Stock image search failed: {e}")

        if not results:
            return self._success(
                "No stock photos found (no provider key configured, or no match). "
                "Proceed without imagery."
            )
        return self._success(json.dumps(results, separators=(",", ":")))


__all__ = ["search_stock_images", "StockImageTool"]
