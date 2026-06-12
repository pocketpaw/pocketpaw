# Link-unfurl router — GET /api/v1/unfurl?url=... returns Open Graph preview
# metadata (title/description/image/site_name/favicon) for a pasted URL.
# Created: 2026-06-10 — the paw-enterprise composer cannot fetch third-party
#   pages itself (CORS), so the backend scrapes the OG tags. Fetching reuses
#   the SSRF-guarded streaming path in pocketpaw.security.safe_fetch (DNS
#   pinned per hop, IP validated public, Host/SNI preserved, 5-hop redirect
#   cap, ~512KB body cut, text/html-only, ~8s total timeout). Metadata is
#   parsed with the stdlib html.parser (no new dependencies). A 15-minute
#   in-process TTL cache (cap 500 entries) keyed by normalized URL keeps chat
#   sessions from hammering the same links.
#
# Wire contract (frozen — frontend built against it in parallel):
#   GET /api/v1/unfurl?url=<urlencoded>
#     200 {"url","title","description","image","site_name","favicon"}
#     400 detail "invalid_url"  — non-http(s) / unparseable
#     400 detail "unsafe_url"   — SSRF-blocked (internal/loopback/private)
#     502 detail "fetch_failed" — timeout / connection error / non-HTML /
#                                 oversized / DNS failure
#   Never 500. All metadata fields nullable; an all-null 200 is valid.

from __future__ import annotations

import logging
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.unfurl import UnfurlResponse
from pocketpaw.security.safe_fetch import (
    BlockedURLError,
    FetchFailedError,
    UnsupportedSchemeError,
    safe_get_streamed,
)
from pocketpaw.security.url_validators import host_is_internal
from pocketpaw.tools.builtin.url_extract import _extract_title

logger = logging.getLogger(__name__)

# Fetch limits — see file-top comment for the contract rationale.
_MAX_BODY_BYTES = 512 * 1024  # ~512 KB
_TOTAL_TIMEOUT_SECONDS = 8.0

# In-process TTL cache.
_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
_CACHE_MAX_ENTRIES = 500
# url -> (stored_monotonic, UnfurlResponse). Module-level: one cache per
# process, shared across requests, which is exactly what we want so repeated
# pastes of the same link in a chat session hit the cache.
_cache: dict[str, tuple[float, UnfurlResponse]] = {}


# A read endpoint serving page metadata — sits alongside files:read, the
# scope the sibling read routers already use. No new scope system invented.
router = APIRouter(tags=["Unfurl"], dependencies=[Depends(require_scope("files:read"))])


def _normalize_cache_key(url: str) -> str:
    """Normalize a URL for cache keying: lowercase scheme+host, drop the
    fragment (it never reaches the server and never changes the metadata)."""
    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",  # strip fragment
        )
    )


def _cache_get(key: str) -> UnfurlResponse | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: UnfurlResponse) -> None:
    # Drop expired entries opportunistically, then evict oldest if still over
    # the cap. Simple insertion-order eviction — dict preserves insertion order
    # in CPython 3.7+, so the first key is the oldest stored.
    now = time.monotonic()
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        expired = [k for k, (ts, _) in _cache.items() if now - ts > _CACHE_TTL_SECONDS]
        for k in expired:
            _cache.pop(k, None)
        while len(_cache) >= _CACHE_MAX_ENTRIES:
            oldest = next(iter(_cache))
            _cache.pop(oldest, None)
    _cache[key] = (now, value)


class _MetaParser(HTMLParser):
    """Collect Open Graph / Twitter / standard meta + icon links from HTML.

    Stdlib only. Tolerant: malformed markup, missing attributes, and the
    parser raising on bad input are all swallowed by the caller. We keep the
    first non-empty value seen for each property (OG tags appear in <head>
    near the top, so first-wins matches author intent)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # meta property/name -> content
        self.meta: dict[str, str] = {}
        # icon href, with rel preference order resolved in handle_starttag
        self.icon_href: str | None = None
        self._icon_rank = 0  # higher = more preferred icon rel

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            content = attr.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rel = attr.get("rel", "").strip().lower()
            href = attr.get("href", "").strip()
            if not href:
                return
            # Prefer a plain "icon"/"shortcut icon" over apple-touch-icon, and
            # any declared icon over none. Rank keeps the best one.
            rank = 0
            if "icon" in rel.split():
                rank = 3 if rel in ("icon", "shortcut icon") else 2
            elif "apple-touch-icon" in rel:
                rank = 1
            if rank > self._icon_rank:
                self._icon_rank = rank
                self.icon_href = href


def _first(meta: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        val = meta.get(key)
        if val:
            return val
    return None


def _absolutize(base_url: str, value: str | None) -> str | None:
    """Resolve a possibly-relative URL against the final page URL.

    Returns None for empty input or anything that doesn't resolve to an
    http(s) URL (e.g. data: URIs we won't echo back as an image)."""
    if not value:
        return None
    try:
        resolved = urljoin(base_url, value)
    except ValueError:
        return None
    scheme = urlsplit(resolved).scheme.lower()
    if scheme not in ("http", "https"):
        return None
    return resolved


def _parse_metadata(html: str, final_url: str) -> UnfurlResponse:
    """Parse OG/Twitter/standard metadata out of an HTML document.

    Mapping (first non-empty wins per field):
      title       og:title -> twitter:title -> <title>
      description og:description -> twitter:description -> meta description
      image       og:image -> og:image:url -> twitter:image -> twitter:image:src
      site_name   og:site_name -> twitter:site
      favicon     <link rel=icon|shortcut icon|apple-touch-icon> href
    Image and favicon are resolved to absolute URLs against ``final_url``."""
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — never let a malformed page 500
        logger.debug("HTML parse raised; using whatever metadata was collected", exc_info=True)
    meta = parser.meta

    title = _first(meta, "og:title", "twitter:title")
    if not title:
        extracted = _extract_title(html)
        # _extract_title falls back to the literal "Untitled" sentinel — map
        # that to None so the contract's "no title" case stays null.
        title = extracted if extracted and extracted != "Untitled" else None

    description = _first(meta, "og:description", "twitter:description", "description")
    site_name = _first(meta, "og:site_name", "twitter:site")
    image = _absolutize(
        final_url, _first(meta, "og:image", "og:image:url", "twitter:image", "twitter:image:src")
    )
    favicon = _absolutize(final_url, parser.icon_href)

    return UnfurlResponse(
        url=final_url,
        title=title,
        description=description,
        image=image,
        site_name=site_name,
        favicon=favicon,
    )


@router.get("/unfurl", response_model=UnfurlResponse)
async def unfurl(url: str = Query(..., description="The URL to unfurl (urlencoded).")):
    """Fetch a URL server-side and return its Open Graph preview metadata.

    See the file-top comment for the frozen wire contract. This handler must
    never raise a 500 — every failure maps to 400 (invalid_url / unsafe_url)
    or 502 (fetch_failed)."""
    # 1. Validate the URL shape. urlsplit never raises for normal input, but
    #    guard the parse anyway so a pathological string can't 500.
    try:
        parts = urlsplit(url)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_url") from None

    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=400, detail="invalid_url")

    # 2. SSRF pre-check on a literal host (IP / localhost). DNS hostnames are
    #    re-checked at fetch time after resolution by safe_get_streamed.
    if host_is_internal(parts.hostname):
        raise HTTPException(status_code=400, detail="unsafe_url")

    cache_key = _normalize_cache_key(url)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # 3. Fetch (SSRF-guarded stream) + parse.
    try:
        result = await safe_get_streamed(
            url,
            max_bytes=_MAX_BODY_BYTES,
            timeout=_TOTAL_TIMEOUT_SECONDS,
            allowed_content_types=("text/html",),
        )
    except UnsupportedSchemeError:
        # Scheme/host became invalid (e.g. a redirect to a non-http scheme).
        raise HTTPException(status_code=400, detail="invalid_url") from None
    except BlockedURLError:
        # Resolved to a non-public IP, or redirect-rebinding / hop-cap hit.
        raise HTTPException(status_code=400, detail="unsafe_url") from None
    except (FetchFailedError, httpx.HTTPError, OSError):
        # DNS failure, disallowed content-type, timeout, connection reset,
        # malformed redirect — anything that isn't an SSRF block.
        raise HTTPException(status_code=502, detail="fetch_failed") from None
    except Exception:  # noqa: BLE001 — belt-and-braces; never 500
        logger.warning("Unexpected unfurl fetch error", exc_info=True)
        raise HTTPException(status_code=502, detail="fetch_failed") from None

    response = _parse_metadata(result.text, result.final_url)
    _cache_put(cache_key, response)
    return response
