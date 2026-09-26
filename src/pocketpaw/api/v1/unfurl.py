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
# 2026-09-26 (feat/unfurl-richer-previews) — previews for chat messages, not
#   just the composer, so the scrape now behaves like a real link-preview bot:
#   sends a crawler User-Agent (the default python-httpx UA is refused or gets
#   OG-less pages on X, Instagram, Reddit, Amazon…), treats a 4xx/5xx page as
#   fetch_failed instead of unfurling the error page, collapses whitespace and
#   caps title/description length, falls back to itemprop="image" and
#   <link rel="image_src">, and returns ``theme_color`` + ``large_image``.
# 2026-09-26 (fix/unfurl-any-member) — any signed-in cloud user can unfurl.
#   The router used require_scope("files:read"), but on the cloud backend that
#   passes only platform superusers: the EE auth bridge sets full_access for
#   is_superuser alone, and a workspace owner/admin/member has no OSS scopes.
#   So every chat member except the operator got 403 "Missing required scope"
#   and saw no link previews. _require_unfurl_access now also accepts
#   request.state.user_id, which the bridge stamps only after a JWT verifies.
#   API keys and OAuth tokens still need files:read; anonymous callers 403.
#
# Wire contract (frozen — frontend built against it in parallel):
#   GET /api/v1/unfurl?url=<urlencoded>
#     200 {"url","title","description","image","site_name","favicon",
#          "theme_color","large_image"}
#     400 detail "invalid_url"  — non-http(s) / unparseable
#     400 detail "unsafe_url"   — SSRF-blocked (internal/loopback/private)
#     502 detail "fetch_failed" — timeout / connection error / non-HTML /
#                                 oversized / DNS failure / 4xx-5xx status
#   Never 500. All metadata fields nullable; an all-null 200 is valid.

from __future__ import annotations

import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

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

# Identify as a link-preview crawler. Sites that gate OG tags behind a bot
# allow-list key on the "facebookexternalhit" / "TwitterBot" tokens, the same
# convention Telegram, Slack and Discord's previewers lean on.
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PocketPawBot/1.0; +https://github.com/pocketpaw/pocketpaw) "
        "facebookexternalhit/1.1 (like TwitterBot)"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Display caps — OG text is author-controlled and sometimes a whole paragraph.
_MAX_TITLE_CHARS = 300
_MAX_DESCRIPTION_CHARS = 600

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_WHITESPACE_RE = re.compile(r"\s+")
# An og:image at least this wide renders as a hero, narrower as a thumbnail.
_LARGE_IMAGE_MIN_WIDTH = 400

# In-process TTL cache.
_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
_CACHE_MAX_ENTRIES = 500
# url -> (stored_monotonic, UnfurlResponse). Module-level: one cache per
# process, shared across requests, which is exactly what we want so repeated
# pastes of the same link in a chat session hit the cache.
_cache: dict[str, tuple[float, UnfurlResponse]] = {}


# Scope check for API keys / OAuth tokens / trusted sessions — files:read, the
# scope the sibling read routers already use. No new scope system invented.
_files_read = require_scope("files:read")


async def _require_unfurl_access(request: Request) -> None:
    """Any signed-in cloud user, or a caller holding files:read.

    Link previews are shown to every chat member, so a workspace role must not
    matter. ``user_id`` is set by the EE auth bridge only after the JWT
    verifies (never from a header), so it proves a real signed-in user.
    Everyone else goes through the normal fail-closed scope check.
    """
    if getattr(request.state, "user_id", None):
        return
    await _files_read(request)


router = APIRouter(tags=["Unfurl"], dependencies=[Depends(_require_unfurl_access)])


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
        # <link rel="image_src"> — the pre-OG way to name a share image.
        self.image_src: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (
                (attr.get("property") or attr.get("name") or attr.get("itemprop") or "")
                .strip()
                .lower()
            )
            content = attr.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rel = attr.get("rel", "").strip().lower()
            href = attr.get("href", "").strip()
            if not href:
                return
            if rel == "image_src":
                self.image_src = self.image_src or href
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


def _clean_text(value: str | None, limit: int) -> str | None:
    """Collapse whitespace runs and cap to ``limit`` chars (ellipsis on cut)."""
    if not value:
        return None
    text = _WHITESPACE_RE.sub(" ", value).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _theme_color(value: str | None) -> str | None:
    """Accept only a hex colour, so clients can style with it unescaped."""
    if value and _HEX_COLOR_RE.match(value.strip()):
        return value.strip().lower()
    return None


def _positive_int(value: str | None) -> int | None:
    try:
        n = int((value or "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


def _is_large_image(meta: dict[str, str]) -> bool:
    """Hero vs thumbnail. twitter:card is the author's explicit choice; without
    one, a declared og:image:width decides; with neither, assume the common
    1200x630 share image and go large."""
    card = meta.get("twitter:card", "").strip().lower()
    if card in ("summary_large_image", "player"):
        return True
    if card == "summary":
        return False
    width = _positive_int(meta.get("og:image:width"))
    if width is not None:
        return width >= _LARGE_IMAGE_MIN_WIDTH
    return True


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
      image       og:image -> og:image:url -> og:image:secure_url -> twitter:image
                  -> twitter:image:src -> itemprop=image -> <link rel=image_src>
      site_name   og:site_name -> twitter:site
      favicon     <link rel=icon|shortcut icon|apple-touch-icon> href
      theme_color <meta name=theme-color>, hex only
      large_image twitter:card / og:image:width (see _is_large_image)
    Image and favicon are resolved to absolute URLs against ``final_url``.
    Title and description are whitespace-collapsed and length-capped."""
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
        final_url,
        _first(
            meta,
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
            "image",
        )
        or parser.image_src,
    )
    favicon = _absolutize(final_url, parser.icon_href)

    return UnfurlResponse(
        url=final_url,
        title=_clean_text(title, _MAX_TITLE_CHARS),
        description=_clean_text(description, _MAX_DESCRIPTION_CHARS),
        image=image,
        site_name=_clean_text(site_name, _MAX_TITLE_CHARS),
        favicon=favicon,
        theme_color=_theme_color(meta.get("theme-color")),
        large_image=_is_large_image(meta) if image else None,
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
            headers=_REQUEST_HEADERS,
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

    # A 404 / 403 / 500 page still carries a <title> ("Page not found", a
    # login wall…). Previewing it would show the error as if it were the link.
    if result.status_code >= 400:
        raise HTTPException(status_code=502, detail="fetch_failed")

    response = _parse_metadata(result.text, result.final_url)
    _cache_put(cache_key, response)
    return response
