# ee/pocketpaw_ee/sites/sitemap.py — the one sitemap reader for every crawl of a
# customer's site: the grounding crawl (``url_crawler.crawl_site`` with
# ``rank_pages``) and the concierge catalog import (``paw_bar.catalog_import``).
#
# NO I/O HERE. ``sitemap_locs`` is a pure parser; ``sitemap_urls`` walks the
# sitemaps through a ``get`` the caller supplies, so every fetch stays on the
# caller's safe_fetch path (host pin, robots, deadline, byte cap). Do not add an
# httpx call to this file.
#
# THE WALK: the sitemaps robots.txt names (on-host only), else /sitemap.xml; one
# level of sitemap index; at most MAX_SITEMAPS documents; only locs on the one
# host come back. A missing or refused sitemap is an empty answer, never an error.
#
# THE PARSER's two locks against entity expansion: the body must be UTF-8 (no
# NUL, no other declared encoding) and is fed to expat as a str, and expat raises
# on any DOCTYPE, entity declaration or external entity reference, so nothing is
# ever expanded. Only a <loc> directly inside <url> or <sitemap> counts, so an
# image or video extension's <image:loc> is not mistaken for a page.

"""Parse and walk a site's sitemaps (pure parser + caller-supplied fetch)."""

from __future__ import annotations

import codecs
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from xml.parsers import expat

MAX_SITEMAPS = 4  # the index or urlset, plus up to three child sitemaps
MAX_LOCS = 2000  # locs read per document, and returned per walk

_XML_ENCODING_RE = re.compile(r"""\s*<\?xml\b[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")


class _RefusedXML(Exception):
    """A sitemap declared a DTD or an entity: refused, nothing expanded."""


class _EnoughLocs(Exception):
    """``MAX_LOCS`` locs read: stop parsing."""


def _refuse_dtd(*_args: Any) -> None:
    raise _RefusedXML


def _local(tag: str) -> str:
    return tag.rsplit(" ", 1)[-1].lower()


def sitemap_locs(body: bytes) -> tuple[bool, list[str]]:
    """(is_index, <loc> values) of a sitemap; (False, []) when refused."""
    if body.startswith(codecs.BOM_UTF8):
        body = body[len(codecs.BOM_UTF8) :]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False, []
    declared = _XML_ENCODING_RE.match(text)
    if "\x00" in text or (declared and declared.group(1).strip().lower() not in ("utf-8", "utf8")):
        return False, []

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_dtd
    parser.EntityDeclHandler = _refuse_dtd
    parser.UnparsedEntityDeclHandler = _refuse_dtd
    parser.ExternalEntityRefHandler = _refuse_dtd
    parser.buffer_text = True
    root: list[str] = []
    stack: list[str] = []
    locs: list[str] = []
    current: list[str] | None = None

    def start(tag: str, _attrs: Any) -> None:
        nonlocal current
        name = _local(tag)
        if not root:
            root.append(name)
        if name == "loc" and stack and stack[-1] in ("url", "sitemap"):
            current = []
        stack.append(name)

    def data(chunk: str) -> None:
        if current is not None:
            current.append(chunk)

    def end(tag: str) -> None:
        nonlocal current
        stack.pop()
        if current is not None and _local(tag) == "loc":
            loc = "".join(current).strip()
            current = None
            if loc:
                locs.append(loc)
                if len(locs) >= MAX_LOCS:
                    raise _EnoughLocs

    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    try:
        parser.Parse(text, True)
    except _EnoughLocs:
        pass
    except (_RefusedXML, expat.ExpatError):
        return False, []
    return bool(root) and root[0] == "sitemapindex", locs


async def sitemap_urls(
    get: Callable[[str], Awaitable[Any]],
    declared: list[str],
    host: str,
    fallback: str,
    *,
    pick_children: Callable[[list[str]], list[str]] | None = None,
) -> list[str]:
    """On-host page URLs from the sitemaps, in sitemap order (at most MAX_LOCS).

    ``get(url)`` returns a FetchResult-like object (``.status``, ``.body``) or
    None; whatever it raises propagates, so a caller's byte budget or deadline
    still stops the walk. ``declared`` is robots.txt's Sitemap: list; ``fallback``
    is the /sitemap.xml URL tried when none of them is on ``host``.
    ``pick_children`` reorders an index's child sitemaps before the cap applies."""

    def on_host(urls: list[str]) -> list[str]:
        return [u for u in urls if (urlsplit(u).hostname or "") == host]

    queue = on_host(declared) or [fallback]
    pages: list[str] = []
    fetched = 0
    while queue and fetched < MAX_SITEMAPS and len(pages) < MAX_LOCS:
        sitemap = queue.pop(0)
        fetched += 1
        result = await get(sitemap)
        if result is None or result.status != 200:
            continue
        is_index, locs = sitemap_locs(result.body)
        if is_index:
            if fetched == 1:  # one level of index only
                children = on_host(locs)
                queue = (pick_children(children) if pick_children else children)[: MAX_SITEMAPS - 1]
            continue
        pages.extend(on_host(locs))
    return pages[:MAX_LOCS]
