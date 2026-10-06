# ee/pocketpaw_ee/sites/favicon.py — a site's gallery card wears the site's own
# mark, not a globe. Finds the icon a page declares and records it on the Site.
#
# NOT THE SCREENSHOT LANE: an icon is a string in the markup, so finding it costs
# one GET (or nothing, for a data: URI) and needs no Browser Rendering. Same three
# rules as that lane though: never blocks a publish, never raises into one (the
# ``safe_`` wrapper), never gates anything. Tests patch the module-attribute
# scheduler to run inline; the strong-ref task set keeps detached tasks alive.
#
# THE VALUE IS A data: URI, capped at ``_MAX_ICON_BYTES``: no blob row, no
# per-card auth grant, no third-party hot-link. An icon over the cap is dropped
# (the card keeps its globe) rather than stored somewhere else.
#
# SSRF. Hrefs come out of markup that, for an imported or connected site, a third
# party wrote. ``_same_origin`` fetches a candidate ONLY when its host equals the
# site's own; data: URIs reach no network. HOW the fetch is made depends on whose
# host it is: a hosted site's address is one we composed, so ``_plain_get`` (plain
# httpx) is fine; a CONNECTED site's host is the customer's, so ``foreign=True``
# routes every request through ``safe_fetch.fetch_single_url`` (``_safe_get``):
# DNS pinned to a validated public IP, every redirect hop re-checked. A foreign
# host must never reach ``_plain_get``.
#
# SVG SAFETY: the card draws through <img src> (scripts do not run there) and
# ``_svg_is_inert`` independently keeps active content out of the field.
#
# CANDIDATES, best first (``extract_icon_candidates``): a scalable SVG icon,
# apple-touch-icon, rel=icon by declared size, msapplication-TileImage,
# mask-icon, the web-app manifest's icons (one extra GET), then /favicon.ico.
# og:image is deliberately excluded: a 1200x630 banner is not a mark.
#
# ABSENCE IS AUTHORITATIVE only when the page was actually read: markup with no
# icon CLEARS the field, a failed fetch leaves it alone. The draft lane passes
# ``clear_when_absent=False`` because assembled draft markup drops icon links.

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

logger = logging.getLogger(__name__)

# The decoded icon's byte ceiling. 8 KB comfortably holds an SVG mark (a few
# hundred bytes to ~2 KB) and a 64-128px PNG, and bounds what N cards add to one
# list response: 30 sites x 8 KB raw is ~330 KB of base64 worst case, and real
# icons land far under it. An icon over the cap is dropped — see the header.
_MAX_ICON_BYTES = 8 * 1024

# How much of a page we are willing to read to find a <link> in its head. The head
# is at the top by definition; a document that has not declared its icon in the
# first 512 KB is not going to.
_MAX_MARKUP_BYTES = 512 * 1024

# A manifest is a small JSON document. This is a sanity bound, not a real limit.
_MAX_MANIFEST_BYTES = 128 * 1024

_FETCH_TIMEOUT = 5.0

# Named in an operator's access log next to the screenshot lane's probe UA.
_FETCH_UA = "PocketPaw-SiteFavicon/1.0 (+card-icon)"

# What we are willing to put on a card. Anything the BYTES are not is dropped —
# ``_sniff_image_mime`` reads magic numbers and never falls back to the
# Content-Type the other end claimed, because for an imported site that header is
# as attacker-controlled as the bytes.
_ALLOWED_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/x-icon",
        "image/svg+xml",
    }
)

_TAG_RE = re.compile(r"<(link|meta)\b[^>]*>", re.IGNORECASE)


def _attr(tag: str, name: str) -> str:
    """One attribute off a raw tag, quoted or bare. "" when absent."""
    m = re.search(
        r"\b" + name + r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
        tag,
        re.IGNORECASE,
    )
    if not m:
        return ""
    return (m.group(1) or m.group(2) or m.group(3) or "").strip()


def _rel_tokens(tag: str) -> set[str]:
    """``rel`` is a space-separated TOKEN LIST, so rel="shortcut icon" is both
    "shortcut" and "icon" and a substring match on it would also fire on
    rel="apple-touch-icon". Tokenise, never substring."""
    return {t for t in _attr(tag, "rel").lower().split() if t}


def _largest_size(sizes: str) -> int:
    """The biggest width in a ``sizes`` list ("32x32 16x16" -> 32). ``any`` means a
    scalable icon, which outranks every raster size."""
    s = sizes.strip().lower()
    if not s:
        return 0
    if "any" in s.split():
        return 1 << 20
    best = 0
    for token in s.split():
        m = re.match(r"^(\d+)x(\d+)$", token)
        if m:
            best = max(best, int(m.group(1)))
    return best


@dataclass(frozen=True)
class IconCandidate:
    """One declared icon, with everything needed to rank it before fetching it."""

    href: str
    source: str
    tier: int
    size: int = 0


# Tiers. Within a tier the largest declared size wins, so a <link rel=icon
# sizes="192x192"> beats an unsized one without needing its own tier.
_TIER_SVG = 0
_TIER_RASTER = 1
_TIER_TILE = 2
_TIER_MASK = 3
_TIER_MANIFEST = 4

# apple-touch-icon is conventionally 180x180 and is reliably square with no
# transparency, so an unsized one is assumed large rather than assumed small. An
# unsized rel=icon gets 0 and sorts behind it — in the wild it is usually the 32px
# one or the .ico.
_APPLE_ASSUMED_SIZE = 180


def _looks_svg(href: str, declared_type: str) -> bool:
    if declared_type.lower().startswith("image/svg"):
        return True
    if href[:14].lower().startswith("data:image/svg"):
        return True
    return urlsplit(href).path.lower().endswith(".svg")


def extract_icon_candidates(markup: str) -> list[IconCandidate]:
    """Every icon this document declares, best first. Pure — no network, no I/O.

    Ranked by :data:`_TIER_SVG` … :data:`_TIER_MANIFEST` and then by declared size
    descending; see the module header for what each tier is and why og:image is not
    among them. The ``/favicon.ico`` guess is NOT added here — it is not a
    declaration, and this function reports what the page said.
    """
    out: list[IconCandidate] = []
    for m in _TAG_RE.finditer(markup or ""):
        tag = m.group(0)
        kind = m.group(1).lower()

        if kind == "meta":
            name = _attr(tag, "name").lower() or _attr(tag, "property").lower()
            if name == "msapplication-tileimage":
                href = _attr(tag, "content")
                if href:
                    out.append(IconCandidate(href, "msapplication-tile", _TIER_TILE))
            continue

        rels = _rel_tokens(tag)
        href = _attr(tag, "href")
        if not href:
            continue
        declared_type = _attr(tag, "type")
        sizes = _largest_size(_attr(tag, "sizes"))

        if "manifest" in rels:
            out.append(IconCandidate(href, "manifest", _TIER_MANIFEST))
            continue
        if "mask-icon" in rels:
            out.append(IconCandidate(href, "mask-icon", _TIER_MASK))
            continue

        is_apple = bool(rels & {"apple-touch-icon", "apple-touch-icon-precomposed"})
        is_icon = "icon" in rels
        if not (is_apple or is_icon):
            continue

        if _looks_svg(href, declared_type):
            out.append(IconCandidate(href, "svg-icon", _TIER_SVG, 1 << 20))
            continue
        size = sizes or (_APPLE_ASSUMED_SIZE if is_apple else 0)
        source = "apple-touch-icon" if is_apple else "icon"
        out.append(IconCandidate(href, source, _TIER_RASTER, size))

    out.sort(key=lambda c: (c.tier, -c.size))
    return out


# --------------------------------------------------------------------------- #
# Turning a candidate into bytes we are willing to put on a card
# --------------------------------------------------------------------------- #

_SVG_ACTIVE_RE = re.compile(rb"<script\b|javascript:|\son\w+\s*=", re.IGNORECASE)


def _svg_is_inert(data: bytes) -> bool:
    """True when this SVG carries no script, no javascript: url and no on* handler.

    The SECOND of two independent controls — the card renders through <img src>,
    where SVG script does not execute at all. This one keeps active content out of
    the stored field regardless of who renders it later. See the module header.
    """
    return not _SVG_ACTIVE_RE.search(data)


def _sniff_image_mime(data: bytes) -> str:
    """The mime these BYTES are. "" when they are not an image we accept.

    Magic numbers only. The Content-Type header is never consulted, not even as a
    fallback: on the fetch path it is set by whatever host the imported markup
    pointed at, so trusting it is trusting the thing we are validating. SVG is the
    one format with no magic number, so it is recognised structurally instead.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] == b"ftypavif":
        return "image/avif"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    head = data[:512].lstrip()
    if head.startswith(b"<") and b"<svg" in data[:2048].lower():
        return "image/svg+xml"
    return ""


def _accept(data: bytes) -> str:
    """The mime to publish these bytes under, or "" to drop them. One place, so the
    cap, the format allowlist and the SVG check cannot be applied on one path and
    forgotten on the other."""
    if not data or len(data) > _MAX_ICON_BYTES:
        return ""
    mime = _sniff_image_mime(data)
    if mime not in _ALLOWED_MIMES:
        return ""
    if mime == "image/svg+xml" and not _svg_is_inert(data):
        logger.debug("sites.favicon: dropped an SVG icon carrying active content")
        return ""
    return mime


def decode_data_uri(href: str) -> tuple[bytes, str]:
    """(bytes, declared-mime) for a data: URI, or (b"", "") when it is not one or
    will not decode. Handles both the base64 and the percent-encoded forms — a
    hand-written SVG favicon is usually the latter."""
    if href[:5].lower() != "data:":
        return b"", ""
    head, sep, payload = href[5:].partition(",")
    if not sep:
        return b"", ""
    params = head.split(";")
    mime = (params[0] or "").strip().lower()
    is_b64 = any(p.strip().lower() == "base64" for p in params[1:])
    try:
        raw = base64.b64decode(payload, validate=False) if is_b64 else unquote_to_bytes(payload)
    except Exception:  # noqa: BLE001 — a malformed icon is a skip, not a failure
        return b"", ""
    return raw, mime


def _same_origin(href: str, base_url: str) -> bool:
    """True when ``href`` resolves onto the SAME host as the site itself.

    The SSRF gate — see the module header. The scheme must be http(s) (so a
    ``file:`` / ``gopher:`` / ``javascript:`` href is refused outright) and the
    netloc must match the site's, case-insensitively. The port is part of that
    comparison, because a different port on the same host is a different service.
    """
    if not base_url:
        return False
    try:
        target = urlsplit(urljoin(base_url, href))
        base = urlsplit(base_url)
    except Exception:  # noqa: BLE001
        return False
    if target.scheme not in ("http", "https"):
        return False
    return target.netloc.lower() == base.netloc.lower()


async def _get(
    url: str,
    *,
    limit: int,
    transport: Any = None,
    foreign: bool = False,
    resolver: Any = None,
) -> bytes:
    """One capped GET. b"" on anything that is not a 2xx, and never raises — every
    failure here is "this site has no icon we can use", not an error to report.

    ``foreign`` says the host is a customer's (a connected site): the request then
    goes through the SSRF-hardened ``_safe_get`` and never the plain client."""
    if foreign:
        return await _safe_get(url, limit=limit, transport=transport, resolver=resolver)
    return await _plain_get(url, limit=limit, transport=transport)


async def _safe_get(url: str, *, limit: int, transport: Any = None, resolver: Any = None) -> bytes:
    """The customer-host GET: ``safe_fetch.fetch_single_url`` (DNS pinned, private
    targets refused, every redirect hop re-checked). An oversized body raises there
    mid-stream, which lands here as b"" like every other refusal."""
    from pocketpaw_ee.sites.safe_fetch import fetch_single_url

    try:
        result = await fetch_single_url(
            url,
            max_bytes=limit,
            timeout_sec=_FETCH_TIMEOUT,
            user_agent=_FETCH_UA,
            transport=transport,
            resolver=resolver,
        )
    except Exception:  # noqa: BLE001 — refused or unreachable is "no icon"
        return b""
    if result.status // 100 != 2:
        return b""
    return result.body


async def _plain_get(url: str, *, limit: int, transport: Any = None) -> bytes:
    """The hosted-site GET, for an address we composed. Never a customer's host."""
    import httpx

    kwargs: dict[str, Any] = {"timeout": _FETCH_TIMEOUT, "follow_redirects": True}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            async with client.stream("GET", url, headers={"user-agent": _FETCH_UA}) as resp:
                if resp.status_code // 100 != 2:
                    return b""
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > limit:
                        # Over budget: stop pulling and drop it. Truncated bytes are
                        # not a smaller icon, they are a corrupt one.
                        return b""
                return bytes(buf)
    except Exception:  # noqa: BLE001 — unreachable is a NO, not a failure
        return b""


def _as_data_uri(data: bytes, mime: str) -> str:
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


async def _resolve_one(
    cand: IconCandidate,
    *,
    base_url: str,
    transport: Any,
    foreign: bool = False,
    resolver: Any = None,
) -> str:
    """One candidate -> a data: URI to store, or "" to move on to the next."""
    href = cand.href.strip()
    if not href:
        return ""

    if href[:5].lower() == "data:":
        raw, _declared = decode_data_uri(href)
        if not _accept(raw):
            return ""
        # Pass the ORIGINAL through rather than re-encoding: a percent-encoded SVG
        # is smaller than its base64 form, and re-encoding would change a value
        # clients may be caching on equality.
        return href

    if not _same_origin(href, base_url):
        logger.debug("sites.favicon: skipped off-origin icon %r", href[:120])
        return ""

    absolute = urljoin(base_url, href)
    data = await _get(
        absolute,
        limit=_MAX_ICON_BYTES,
        transport=transport,
        foreign=foreign,
        resolver=resolver,
    )
    mime = _accept(data)
    if not mime:
        return ""
    return _as_data_uri(data, mime)


async def _resolve_manifest(
    cand: IconCandidate,
    *,
    base_url: str,
    transport: Any,
    foreign: bool = False,
    resolver: Any = None,
) -> str:
    """A web-app manifest's best icon. One extra same-origin GET for the JSON and one
    for the image it names, and every gate the declared path uses applies to both."""
    if not _same_origin(cand.href, base_url):
        return ""
    manifest_url = urljoin(base_url, cand.href)
    body = await _get(
        manifest_url,
        limit=_MAX_MANIFEST_BYTES,
        transport=transport,
        foreign=foreign,
        resolver=resolver,
    )
    if not body:
        return ""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a broken manifest is a skip
        return ""
    icons = doc.get("icons") if isinstance(doc, dict) else None
    if not isinstance(icons, list):
        return ""

    ranked: list[tuple[int, str]] = []
    for entry in icons:
        if not isinstance(entry, dict):
            continue
        src = entry.get("src")
        if not isinstance(src, str) or not src.strip():
            continue
        ranked.append((_largest_size(str(entry.get("sizes") or "")), src.strip()))
    ranked.sort(key=lambda p: -p[0])

    for _size, src in ranked:
        # An icon src is relative to the MANIFEST, not to the document.
        inner = IconCandidate(urljoin(manifest_url, src), "manifest-icon", _TIER_MANIFEST)
        got = await _resolve_one(
            inner, base_url=base_url, transport=transport, foreign=foreign, resolver=resolver
        )
        if got:
            return got
    return ""


async def resolve_favicon(
    markup: str,
    *,
    base_url: str = "",
    transport: Any = None,
    foreign: bool = False,
    resolver: Any = None,
) -> str:
    """This document's best usable icon as a data: URI, or "" when it has none.

    Walks :func:`extract_icon_candidates` in order and returns the first candidate
    that survives every gate, then falls back to the ``/favicon.ico`` guess. With no
    ``base_url`` only data: URIs can resolve — nothing else has an origin to be
    same-origin with — which is exactly the draft case.

    ``foreign`` marks ``base_url`` as a customer's host (a connected site): every
    request goes through ``safe_fetch``. ``resolver`` is its DNS test seam.
    """
    net = {"transport": transport, "foreign": foreign, "resolver": resolver}
    for cand in extract_icon_candidates(markup):
        if cand.source == "manifest":
            got = await _resolve_manifest(cand, base_url=base_url, **net)
        else:
            got = await _resolve_one(cand, base_url=base_url, **net)
        if got:
            logger.debug("sites.favicon: resolved icon from %s", cand.source)
            return got

    if base_url:
        data = await _get(urljoin(base_url, "/favicon.ico"), limit=_MAX_ICON_BYTES, **net)
        mime = _accept(data)
        if mime:
            logger.debug("sites.favicon: resolved icon from /favicon.ico")
            return _as_data_uri(data, mime)
    return ""


async def fetch_markup(url: str, *, transport: Any = None) -> str | None:
    """The page's HTML, or None when it could not be read.

    None and "" are DIFFERENT answers and the caller depends on it: None is "we
    never saw the page" (leave the stored icon alone) and "" is a page that served
    us nothing (treat it as declaring no icon). See the header's absence rule.
    """
    data = await _get(url, limit=_MAX_MARKUP_BYTES, transport=transport)
    if not data:
        return None
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Recording it on the Site
# --------------------------------------------------------------------------- #


async def take_site_favicon(
    site: Any,
    *,
    markup: str | None = None,
    transport: Any = None,
    clear_when_absent: bool = True,
) -> str:
    """Find this site's icon and record it on the Site. Returns the stored data URI,
    or "" when there is none.

    ``markup`` lets a caller that ALREADY has the document hand it over — the draft
    lane has just assembled one and must not pay for it twice. Without it the site's
    own live url is fetched, which is also what supplies the ``base_url`` every
    non-data: candidate is resolved and origin-checked against.

    ``clear_when_absent`` is the draft lane's escape hatch, and the header explains
    why it exists: assembled draft markup has had its local icon links stripped, so
    absence there is not evidence.

    Written with a targeted ``set()``, never ``save()`` — same reason as the
    screenshot lane: this lands after the publish that scheduled it, holding a doc
    snapshotted before it, so a whole-document write would roll back anything
    committed in between.
    """
    url = (getattr(site, "url", "") or "").strip()
    if markup is None:
        if not url:
            return ""
        fetched = await fetch_markup(url, transport=transport)
        if fetched is None:
            # We never saw the page. Not evidence of anything — keep what is stored.
            logger.debug(
                "sites.favicon: could not read %s — leaving site %s's icon alone",
                url,
                getattr(site, "id", "?"),
            )
            return ""
        markup = fetched

    icon = await resolve_favicon(markup, base_url=url, transport=transport)
    if not icon:
        if clear_when_absent and (getattr(site, "favicon_url", "") or ""):
            await site.set({"favicon_url": ""})
            logger.info(
                "sites.favicon: site %s no longer declares an icon — cleared",
                getattr(site, "id", "?"),
            )
        return ""

    if (getattr(site, "favicon_url", "") or "") == icon:
        # Unchanged. Skipping the write keeps a republish from touching the document
        # for nothing; the value is content-addressed by construction.
        return icon

    await site.set({"favicon_url": icon})
    logger.info("sites.favicon: recorded icon for site %s", getattr(site, "id", "?"))
    return icon


async def safe_take_site_favicon(
    site: Any,
    *,
    markup: str | None = None,
    transport: Any = None,
    clear_when_absent: bool = True,
) -> str:
    """:func:`take_site_favicon` that never raises — the form every caller on a
    publish, create or import path uses. A card with a globe is the pre-existing
    card; it is not worth failing anything over."""
    try:
        return await take_site_favicon(
            site,
            markup=markup,
            transport=transport,
            clear_when_absent=clear_when_absent,
        )
    except Exception:  # noqa: BLE001 — an icon is never a gate on a publish
        logger.warning(
            "sites.favicon: lookup failed for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )
        return ""


# Background-task keepalive — see the module header. Its own set rather than the
# screenshot lane's, so a test that drains one lane cannot silently drain the other.
_FAVICON_TASKS: set[asyncio.Task[Any]] = set()


def _default_favicon_scheduler(coro: Any) -> None:
    """Detach onto the running loop and return. With no running loop the coroutine
    is closed and skipped. Tests patch this module attribute to run it inline."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _FAVICON_TASKS.add(task)
    task.add_done_callback(_FAVICON_TASKS.discard)


def schedule_site_favicon(site: Any, *, markup: str | None = None) -> None:
    """Fire a background icon lookup for a site. Never blocks, never raises."""
    _default_favicon_scheduler(safe_take_site_favicon(site, markup=markup))


def schedule_draft_favicon(site: Any, *, markup: str) -> None:
    """Record a DRAFT's icon from markup the caller already has. Never blocks, never
    raises, and never clears — see ``clear_when_absent``."""
    _default_favicon_scheduler(safe_take_site_favicon(site, markup=markup, clear_when_absent=False))


__all__ = [
    "IconCandidate",
    "decode_data_uri",
    "extract_icon_candidates",
    "fetch_markup",
    "resolve_favicon",
    "safe_take_site_favicon",
    "schedule_draft_favicon",
    "schedule_site_favicon",
    "take_site_favicon",
]
