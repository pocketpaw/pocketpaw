# ee/pocketpaw_ee/integrations/providers/_commerce.py: what checkout providers share.
#
# The pieces every "sell one catalog product" provider (stripe, razorpay) needs:
#   * ``CheckoutArgs``      the public action's args: product_ref + qty (1..99), and
#                           nothing else, so a caller can never send a price
#   * ``catalog_product``   the price source: the site pocket's Paw Bar catalog row,
#                           strictly this workspace's widgets (legacy ''/NULL rows
#                           never price a checkout)
#   * ``site_origin``       where buyers come back to: a live custom domain first,
#                           else the deployed url
#   * ``safe_path``         an owner-set return path; anything that isn't a plain
#                           same-origin path reads as "/"
# Amounts stay ISO 4217 minor units (``price_cents``) end to end.

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from pocketpaw_ee.integrations.providers.base import ActionArgs

MAX_PATH = 200


class CheckoutArgs(ActionArgs):
    product_ref: str = Field(min_length=1, max_length=120)
    qty: int = Field(default=1, ge=1, le=99)


def safe_path(value: Any) -> str:
    """An owner path ("/thanks"), never a URL: anything else reads as "/"."""
    if not isinstance(value, str):
        return "/"
    path = value.strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or len(path) > MAX_PATH
        or any(c in "\\\"'<>` " or ord(c) < 0x20 or ord(c) == 0x7F for c in path)
        or not path.isascii()
    ):
        return "/"
    return path


def _origin_of(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


async def site_origin(site_id: str) -> str:
    """The site's public origin: a live custom domain first, else its deployed url."""
    from pocketpaw_ee.integrations.gateway import _site_by_id

    site = await _site_by_id(site_id)
    if site is None:
        return ""
    for domain in site.domains or []:
        if domain.status == "live" and domain.hostname:
            origin = _origin_of(f"https://{domain.hostname}")
            if origin:
                return origin
    return _origin_of(site.url or "")


async def catalog_product(workspace_id: str, pocket_id: str, ref: str):
    """The catalog row for ``ref`` across the site pocket's Paw Bar widgets, or None.

    Only widgets stamped with THIS workspace count: the store's scoped read also
    matches legacy ''/NULL rows, which must never price a checkout."""
    from pocketpaw.stores import get_paw_bar_store

    if not pocket_id or not workspace_id:
        return None
    store = get_paw_bar_store()
    widgets = await store.list_widgets(pocket_id=pocket_id, workspace_id=workspace_id)
    for widget in widgets:
        if widget.pocket_id != pocket_id or widget.workspace_id != workspace_id:
            continue
        rows = await store.get_catalog_items(widget.id, [ref], workspace_id=workspace_id)
        if rows:
            return rows[0]
    return None
