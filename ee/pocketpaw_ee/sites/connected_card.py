# ee/pocketpaw_ee/sites/connected_card.py — the gallery card for a CONNECTED site
# (``Site.foreign_origin``): the customer's own page title, icon and screenshot.
#
# A connected site never deploys, so the hosted card lanes (post-deploy screenshot
# and favicon) never run for it. This is its one "card refresh": fetch
# ``https://{verified host}/`` ONCE, read ``origin_title`` (<title>, else og:title)
# and the icon out of that markup, then take the screenshot through the existing
# ``screenshot`` lane. The owner's ``name`` is never touched; the client shows
# ``name || origin_title || host``.
#
# SSRF: the host is the customer's, so the homepage and every icon request go
# through ``safe_fetch.fetch_single_url`` (DNS pinned to a validated public IP,
# every redirect hop re-checked). Only ``foreign_grounding.crawlable_origin``'s
# verified, fresh host is ever addressed, never an unproved ``allowed_origins``
# entry. ``_transport`` / ``_resolver`` are the module's test seams; production
# leaves them None (a real client and real DNS).
#
# Triggers: a first bind, a rebind, an origin (re-)verification, the knowledge
# sync (as a second chance, reusing the crawl's homepage HTML) and the owner's
# synchronous preview-refresh. Background callers use ``schedule_connected_card``,
# which never blocks and never raises; every decline is logged with its reason.
# Writes are targeted ``set()`` calls, never ``save()``, so a refresh landing late
# cannot roll back a concurrent edit.

"""Title, icon and screenshot for a connected (foreign-origin) site's card."""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

# The card renders a line, not a paragraph.
TITLE_MAX_CHARS = 200

# Enough to hold any real <head>; a page over it is refused mid-stream by
# safe_fetch and the card simply keeps what it had.
_HOMEPAGE_MAX_BYTES = 2 * 1024 * 1024
_HOMEPAGE_TIMEOUT_SEC = 10.0

# Named in the customer's access log next to the screenshot probe's UA.
_CARD_UA = "PocketPaw-SiteCard/1.0 (+connected-site-card)"

# Test seams for safe_fetch (an httpx transport and a DNS resolver). None in
# production. Tests point them at a MockTransport and a dict-backed resolver.
_transport: Any = None
_resolver: Any = None


def clean_title(raw: str) -> str:
    """A page title as the card may show it: control and format characters
    dropped, whitespace collapsed, capped at ``TITLE_MAX_CHARS``. "" for none."""
    kept = "".join(
        ch for ch in (raw or "") if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf")
    )
    return " ".join(kept.split())[:TITLE_MAX_CHARS].strip()


def title_from_markup(markup: str) -> str:
    """The page's own name: <title>, else og:title, cleaned. "" when it has none."""
    from pocketpaw_ee.sites.import_service import _MetaScan

    scan = _MetaScan()
    try:
        scan.feed(markup or "")
    except Exception:  # noqa: BLE001 — malformed markup is "no title", not a failure
        logger.debug("sites.connected_card: could not parse homepage markup", exc_info=True)
    return clean_title(scan.title) or clean_title(scan.og_title)


async def _read_homepage(host: str) -> tuple[str, str] | None:
    """(final url, html) for ``https://{host}/``, or None when it could not be read.

    The one customer-host GET of a refresh, through safe_fetch. A refusal (private
    address, bad redirect, too large) and a non-2xx both read as None."""
    from pocketpaw_ee.sites.safe_fetch import fetch_single_url

    url = f"https://{host}/"
    try:
        result = await fetch_single_url(
            url,
            max_bytes=_HOMEPAGE_MAX_BYTES,
            timeout_sec=_HOMEPAGE_TIMEOUT_SEC,
            user_agent=_CARD_UA,
            transport=_transport,
            resolver=_resolver,
        )
    except Exception as exc:  # noqa: BLE001 — refused or unreachable: keep the card
        logger.info(
            "sites.connected_card: could not read %s (%s)",
            url,
            getattr(exc, "code", type(exc).__name__),
        )
        return None
    if result.status // 100 != 2:
        logger.info("sites.connected_card: %s answered %s", url, result.status)
        return None
    return result.url or url, result.body.decode("utf-8", "replace")


async def refresh_card_meta(site: Any, *, markup: str | None = None, base_url: str = "") -> bool:
    """Record ``origin_title`` and ``favicon_url`` for a connected site.

    With ``markup`` (and the ``base_url`` it was read from) no homepage request is
    made: the knowledge sync hands over the crawl's copy. Without it the verified
    origin's homepage is fetched once. Returns True when a page was read and the
    fields were recorded, False when there was nothing to read (no verified fresh
    origin, a refused or failed fetch). Absence is authoritative only for a page
    actually read: a title-less page records "", a failed read writes nothing.
    """
    if not getattr(site, "foreign_origin", False):
        return False
    if markup is None:
        from pocketpaw_ee.sites.foreign_grounding import crawlable_origin

        host, reason = await crawlable_origin(site)
        if not host:
            logger.info(
                "sites.connected_card: site %s has no readable origin (%s)",
                getattr(site, "id", "?"),
                reason,
            )
            return False
        page = await _read_homepage(host)
        if page is None:
            return False
        base_url, markup = page

    from pocketpaw_ee.sites import favicon

    updates: dict[str, Any] = {}
    title = title_from_markup(markup)
    if title != (getattr(site, "origin_title", "") or ""):
        updates["origin_title"] = title
    icon = ""
    if base_url:
        icon = await favicon.resolve_favicon(
            markup, base_url=base_url, transport=_transport, foreign=True, resolver=_resolver
        )
    if icon != (getattr(site, "favicon_url", "") or ""):
        updates["favicon_url"] = icon
    if updates:
        await site.set(updates)
    logger.info(
        "sites.connected_card: refreshed site %s (title=%s icon=%s)",
        getattr(site, "id", "?"),
        "yes" if title else "none",
        "yes" if icon else "none",
    )
    return True


async def refresh_connected_card(
    site: Any, *, markup: str | None = None, base_url: str = "", screenshot: bool = True
) -> None:
    """The background card refresh: title + icon, then the screenshot. Never raises."""
    try:
        await refresh_card_meta(site, markup=markup, base_url=base_url)
    except Exception:  # noqa: BLE001 — a card is never a gate on anything
        logger.warning(
            "sites.connected_card: title/icon refresh failed for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )
    if not screenshot:
        return
    from pocketpaw_ee.sites import screenshot as screenshot_mod

    await screenshot_mod.safe_take_site_screenshot(site)


# Background-task keepalive: asyncio holds only a WEAK ref to a bare create_task.
_CARD_TASKS: set[asyncio.Task[Any]] = set()


def _default_card_scheduler(coro: Any) -> None:
    """Detach onto the running loop. With no loop the coroutine is closed and
    skipped. Tests patch this module attribute to record or run it inline."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _CARD_TASKS.add(task)
    task.add_done_callback(_CARD_TASKS.discard)


def schedule_connected_card(
    site: Any, *, markup: str | None = None, base_url: str = "", screenshot: bool = True
) -> None:
    """Fire a background card refresh for a connected site. Never blocks, never raises."""
    try:
        _default_card_scheduler(
            refresh_connected_card(site, markup=markup, base_url=base_url, screenshot=screenshot)
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "sites.connected_card: could not schedule a refresh for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


__all__ = [
    "TITLE_MAX_CHARS",
    "clean_title",
    "refresh_card_meta",
    "refresh_connected_card",
    "schedule_connected_card",
    "title_from_markup",
]
