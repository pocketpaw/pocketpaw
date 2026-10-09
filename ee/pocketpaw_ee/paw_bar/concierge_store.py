# ee/pocketpaw_ee/paw_bar/concierge_store.py — the ripple concierge's store data.
#
# An ops site on the ripple profile may name a store (``Site.concierge_store_url``,
# gated per turn by ``concierge_runtime.storefront_for_turn``). ``load_store`` reads
# /api/store (timezone, fulfilment, delivery fee), /api/menu and
# /api/booking/services, then /api/booking/slots for each of the next 7 days in the
# store's timezone, all inside one ``_TIMEOUT_S`` budget; ``cached_store`` keeps the
# result ``_TTL_S`` per site (``_MISS_TTL_S`` when nothing answered).
#
# The result, ``StoreData``, is plain values already in the widgets' shapes
# (ripple menu-order items, booking services and days) and already cleaned, since
# the hydrated card is not walked again: text is plain (no link, script scheme,
# brace or fence), a price is a number, an image is https on the store's own host
# or ``images.unsplash.com``, else absent. ``card_spec`` hydrates cards from it
# and ``concierge_runtime`` writes its menu as the <store-menu> block.
#
# Production fetches go through ``pocketpaw.security.safe_fetch.safe_get_streamed``
# (DNS pinned, public IPs only on every hop, so never loopback), JSON only, capped
# at ``_MAX_BYTES``. ``fetch`` is injectable for a local harness and tests; the
# runtime never passes one. A part that fails is None; nothing here raises, and
# failures are logged by exception type only.

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TIMEOUT_S = 2.0
_TTL_S = 60.0
_MISS_TTL_S = 10.0
_MAX_BYTES = 512_000
_DAYS = 7
_MAX_PRODUCTS = 120
_MAX_SLOTS = 96
_CACHE_SITES = 64
# Menu photos may come from the store's own host or Unsplash (captain, 2026-10-09).
_IMAGE_HOSTS = frozenset({"images.unsplash.com"})
# The store's category ids, as the menu-order widget's row kinds (else "main").
_KINDS = {"appetizers": "side", "sides": "side", "drinks": "drink", "desserts": "dessert"}
_SERVICE_KINDS = frozenset({"table", "hair", "beauty", "health", "class", "other"})
_PRICE = re.compile(r"\d{1,6}(?:\.\d{1,2})?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_START = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})")
_TZ = re.compile(r"[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){0,2}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_URL_BAD = frozenset(" \"'<>\\`(){}[]|^")


@dataclass(frozen=True)
class StoreData:
    """One store, read once. ``products`` maps a product id to its menu-order item
    (None: the menu did not answer); ``days`` the next 7 days of slots for the
    first service (None: no slots answered). Hydration copies, never mutates."""

    host: str
    products: dict[str, dict[str, Any]] | None = None
    currency: str = "USD"
    fulfilment: tuple[str, ...] = ("pickup",)
    delivery_fee: float | None = None
    services: tuple[dict[str, Any], ...] | None = None
    days: tuple[dict[str, Any], ...] | None = None
    tz: str = ""
    today: str = ""

    @property
    def reachable(self) -> bool:
        return self.products is not None or self.services is not None


async def _safe_json(url: str) -> Any:
    """The production fetch: the SSRF-safe pinned GET, JSON only, capped."""
    from pocketpaw.security.safe_fetch import safe_get_streamed

    result = await safe_get_streamed(
        url,
        max_bytes=_MAX_BYTES,
        timeout=_TIMEOUT_S,
        allowed_content_types=("application/json",),
    )
    if not 200 <= result.status_code < 300 or result.truncated:
        raise ValueError(f"store answered {result.status_code}")
    return json.loads(result.text)


def plain(value: Any, limit: int) -> str:
    """Store text fit for a card: one line, at most ``limit`` chars, "" when it
    holds a brace, a fence, a link or a script scheme (``card_spec._check_text``)."""
    from pocketpaw_ee.paw_bar.card_spec import _check_text, _Reject

    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())[:limit]
    if any(c in text for c in "{}`"):
        return ""
    try:
        _check_text(text, frozenset())
    except _Reject:
        return ""
    return text


def _number(value: Any) -> float | None:
    """A price or delta: a finite number from 0 up, or a "11.99" string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and _PRICE.fullmatch(value.strip()):
        value = float(value)
    if isinstance(value, int | float) and math.isfinite(value) and 0 <= value < 1_000_000:
        return round(float(value), 2)
    return None


def _image(value: Any, host: str) -> str:
    """An https photo on the store's own host or an allowed photo host, else ""."""
    if not isinstance(value, str) or len(value) > 1000 or not value.isascii():
        return ""
    if not value.startswith("https://") or _URL_BAD & set(value):
        return ""
    parts = urlsplit(value)
    if "@" in parts.netloc or parts.hostname not in (_IMAGE_HOSTS | {host}):
        return ""
    return value


def _groups(raw: Any) -> list[dict[str, Any]]:
    """The store's ``optionGroups`` as the widget's ``groups``."""
    out: list[dict[str, Any]] = []
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict):
            continue
        options = []
        for o in g.get("options") if isinstance(g.get("options"), list) else []:
            if not isinstance(o, dict):
                continue
            oid, name = plain(o.get("id"), 60), plain(o.get("name"), 60)
            if oid and name:
                delta = _number(o.get("price_delta")) or 0.0
                options.append({"id": oid, "name": name, "price_delta": delta})
        gid = plain(g.get("id"), 60)
        if not gid or not options:
            continue
        group: dict[str, Any] = {
            "id": gid,
            "name": plain(g.get("name"), 60) or gid,
            "choose": "many" if g.get("choose") == "many" else "one",
            "options": options,
        }
        if g.get("required") is True:
            group["required"] = True
        cap = g.get("max")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
            group["max"] = cap
        out.append(group)
    return out


def _products(raw: Any, host: str) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """(product id -> menu-order item, currency) from /api/menu. An unavailable,
    unpriced or unnamed product is left out."""
    rows = raw.get("products") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return None, "USD"
    out: dict[str, dict[str, Any]] = {}
    currency = ""
    for p in rows[:_MAX_PRODUCTS]:
        if not isinstance(p, dict) or p.get("available") is False:
            continue
        pid, name = plain(p.get("id"), 80), plain(p.get("name"), 120)
        price = _number(p.get("price"))
        if not pid or not name or price is None or pid in out:
            continue
        item: dict[str, Any] = {"product_id": pid, "name": name, "price": price}
        description = plain(p.get("description"), 300)
        if description:
            item["description"] = description
        image = _image(p.get("image"), host)
        if image:
            item["image"] = image
        category = plain(p.get("category"), 60)
        if category:
            item["category"] = category
        tags = p.get("tags") if isinstance(p.get("tags"), list) else []
        item["tags"] = [t for t in (plain(t, 30) for t in tags[:5]) if t]
        item["kind"] = _KINDS.get(str(p.get("categoryId") or "").lower(), "main")
        item["groups"] = _groups(p.get("optionGroups"))
        out[pid] = item
        if not currency and isinstance(p.get("currency"), str):
            currency = p["currency"] if _CURRENCY.fullmatch(p["currency"]) else ""
    return out, currency or "USD"


def _services(raw: Any) -> tuple[dict[str, Any], ...] | None:
    rows = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return None
    out = []
    for s in rows[:20]:
        if not isinstance(s, dict):
            continue
        sid, name = plain(s.get("id"), 60), plain(s.get("name"), 80)
        duration = s.get("duration_min")
        if not sid or not name or not isinstance(duration, int) or not 0 < duration <= 1440:
            continue
        service: dict[str, Any] = {"id": sid, "name": name, "duration_min": duration}
        if s.get("kind") in _SERVICE_KINDS:
            service["kind"] = s["kind"]
        price = _number(s.get("price"))
        if price is not None:
            service["price"] = price
        party = s.get("party") if isinstance(s.get("party"), dict) else {}
        low, high = party.get("min"), party.get("max")
        if all(isinstance(n, int) and not isinstance(n, bool) for n in (low, high)):
            if 1 <= low <= high <= 100:
                service["party"] = {"min": low, "max": high}
        out.append(service)
    return tuple(out) or None


def _day(raw: Any, date: str) -> dict[str, Any] | None:
    """One /api/booking/slots answer as a widget day, or None when it is not one."""
    if not isinstance(raw, dict) or raw.get("date") != date:
        return None
    if not isinstance(raw.get("slots"), list):
        return None
    slots = []
    for s in raw["slots"][:_MAX_SLOTS]:
        start = s.get("start") if isinstance(s, dict) else None
        if not isinstance(start, str) or not _START.fullmatch(start):
            continue
        label = plain(s.get("label"), 20) or start[11:16]
        slots.append({"start": start, "label": label, "available": s.get("available") is True})
    return {"date": date, "date_label": plain(raw.get("date_label"), 40) or date, "slots": slots}


def _zone(name: Any) -> str:
    if not isinstance(name, str) or not _TZ.fullmatch(name):
        return ""
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone is no zone
        return ""
    return name


async def load_store(base_url: str, *, fetch: Any = None) -> StoreData:
    """The store at ``base_url`` (no trailing /), read now. Never raises."""
    fetch = fetch or _safe_json
    host = urlsplit(base_url).hostname or ""
    deadline = time.monotonic() + _TIMEOUT_S

    async def get(path: str) -> Any:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            return await asyncio.wait_for(fetch(base_url + path), left)
        except Exception as exc:  # noqa: BLE001 — a part that fails is None
            logger.info("concierge store: %s failed (%s)", path.split("?")[0], type(exc).__name__)
            return None

    info, menu, services_raw = await asyncio.gather(
        get("/api/store"), get("/api/menu"), get("/api/booking/services")
    )
    store = info.get("store") if isinstance(info, dict) else None
    store = store if isinstance(store, dict) else {}
    tz = _zone(store.get("timezone"))
    features = store.get("features") if isinstance(store.get("features"), list) else []
    fulfilment = tuple(m for m in ("pickup", "delivery") if m in features) or ("pickup",)
    fee = _number(store.get("deliveryFee")) if "delivery" in fulfilment else None
    products, currency = _products(menu, host)
    services = _services(services_raw)

    today = datetime.now(ZoneInfo(tz) if tz else UTC).date()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(_DAYS)]
    days = None
    if services:
        party = services[0].get("party", {}).get("min", 1)
        replies = await asyncio.gather(
            *(
                get(
                    "/api/booking/slots?"
                    + urlencode({"service": services[0]["id"], "date": d, "party": party})
                )
                for d in dates
            )
        )
        found = [day for day in (_day(r, d) for r, d in zip(replies, dates, strict=True)) if day]
        days = tuple(found) or None
        tz = tz or next((_zone(r.get("tz")) for r in replies if isinstance(r, dict)), "")
    return StoreData(
        host=host,
        products=products,
        currency=currency,
        fulfilment=fulfilment,
        delivery_fee=fee,
        services=services,
        days=days,
        tz=tz,
        today=dates[0],
    )


_CACHE: dict[tuple[str, str], tuple[float, StoreData]] = {}


async def cached_store(site_id: str, base_url: str, *, fetch: Any = None) -> StoreData:
    """``load_store``, kept ``_TTL_S`` per (site, url) (``_MISS_TTL_S`` when nothing
    answered). Two turns racing a miss both fetch; the later one wins."""
    key = (site_id, base_url)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    data = await load_store(base_url, fetch=fetch)
    _CACHE.pop(key, None)
    if len(_CACHE) >= _CACHE_SITES:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (now + (_TTL_S if data.reachable else _MISS_TTL_S), data)
    return data


__all__ = ["StoreData", "cached_store", "load_store", "plain"]
