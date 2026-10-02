# src/pocketpaw/paw_bar/pages.py — the one spelling of "which page is this".
#
# ``page_key(path)`` is the crawl-index key for a site page, shared by the site
# knowledge sync (``pocketpaw_ee.sites.kb_ingest`` re-exports it), the v2
# concierge's visitor-page lookup, and the catalog store's ``page_key`` column.
# It lives in core because the catalog store (``paw_bar.store``) must compute it
# on write and core may not import EE. ``url_page_key`` applies it to a catalog
# item's ``url`` (an absolute http(s) url or a site path).
#
# Changing either function re-keys every stored catalog row and every crawl-index
# entry, so a change needs a data migration.

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


def page_key(path: str) -> str:
    """The crawl-index key for a page: its path with the file extension, SvelteKit
    route scaffolding, a trailing ``index`` and the outer slashes removed, lowercased.

    The sync keys a document by it and the v2 concierge keys the visitor's URL path
    by it, so "about.html", "about/index.html" (the foreign crawler's spelling),
    "src/routes/about/+page.svelte" and "/about/" are one page, and every spelling of
    the homepage is "". It keeps slashes, so "blog/post" and "blog-post" stay two
    pages.
    """
    key = path.strip().lower().lstrip("/")
    key = re.sub(r"^src/routes/", "", key)
    key = re.sub(r"\.(html?|svelte|md|svx)$", "", key)
    key = re.sub(r"(^|/)\+(page|layout)$", "", key)
    key = key.rstrip("/")
    key = re.sub(r"(^|/)index$", "", key)
    return key.strip("/")


def url_page_key(url: str) -> str | None:
    """``page_key`` of a catalog item's url, or None when the url names no page
    (empty, or a scheme other than http(s))."""
    raw = (url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("", "http", "https"):
        return None
    return page_key(unquote(parts.path))


def url_host(url: str) -> str:
    """The lower-cased host of an absolute url, or "" for a site path."""
    return (urlsplit((url or "").strip()).hostname or "").lower()


__all__ = ["page_key", "url_host", "url_page_key"]
