# ee/pocketpaw_ee/paw_bar/catalog_import.py — read a store's products off its own
# website, for the owner to review before they reach the catalog.
#
# PREVIEW ONLY. ``preview_catalog_import`` returns a ``CatalogImportPreview`` and
# writes nothing: the owner picks products in the dashboard and saves them through
# the catalog routes (``POST …/catalog/items:bulk``, 500 per call), whose store
# applies the catalog validators. Results are capped at the catalog cap
# (``catalog_max_items``, config ``pawbar_catalog_max_items``).
#
# Two layers. The PURE parsers (``parse_shopify_products``, ``parse_woo_products``,
# ``parse_jsonld_products``, ``parse_og_product``, ``detect_platform``) take bytes
# or dicts and do no I/O; every product they find goes through ONE normaliser
# (``_normalise``), which converts the decimal price to ISO 4217 minor units of
# its currency (``pocketpaw.money.to_minor``; an unknown currency is "" and
# converts as 2 decimals), strips HTML, caps lengths, keeps only https images and
# on-host links (stored as site paths, the form ``concierge_runtime`` matches the
# visitor's page against), and drops a product with no parseable price. The
# ORCHESTRATOR fetches: Shopify's ``/products.json`` or WooCommerce's Store API
# when the homepage says so, and otherwise (or when that endpoint is refused) the
# generic reader: sitemap → product pages → JSON-LD, then OpenGraph product tags.
# Shopify pages ``/products.json`` up to 20 × 250, WooCommerce up to 50 × 100, both
# stopping at a short page, the catalog cap or the soft deadline (below).
#
# THE FETCH RULES, same as the knowledge crawl (``sites.foreign_grounding``):
#   * a connected (foreign) site is read on its ONE verified, fresh origin
#     (``crawlable_origin``); a hosted Paw Site on its first live custom domain,
#     else its deployed host (``embed.deployed_host(site.url)``), with no
#     ownership check (we deployed it), the generic reader only, and
#     ``site_not_deployed`` when that host is unset or local. Every fetch is
#     pinned to the one host, redirects too;
#   * every request goes through ``safe_fetch.SafeFetcher`` (SSRF pinning, size
#     caps); no httpx call lives here;
#   * robots.txt is checked for EVERY url, under the concierge crawler's UA; a
#     robots.txt that cannot be read (including one redirecting off the host) is
#     allow-all with a ``robots_unreadable`` warning, the crawl's own policy;
#   * one soft deadline (``IMPORT_WALL_CLOCK_SEC``) for the whole run: no fetch
#     starts unless its own timeout fits in what is left, so platform paging and
#     the page walk stop there and return what they read as ``partial`` +
#     ``deadline_reached``; a hard ``asyncio.timeout`` sits
#     ``IMPORT_BACKSTOP_GRACE_SEC`` past it. ``IMPORT_MAX_PAGES`` product pages for
#     the generic reader, ``IMPORT_BYTE_CAP`` bytes for the whole run.
#   * sitemaps are parsed only as UTF-8, by an expat parser that refuses any
#     DOCTYPE or entity declaration (``_sitemap_locs``).
# Never raises: every failure is ``status="failed"`` with a ``reason``.

"""Preview a store's products from its own site (Shopify, Woo, JSON-LD, OG)."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import ipaddress
import json
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit
from xml.parsers import expat

import httpx
from pydantic import BaseModel, Field

from pocketpaw.api.v1.unfurl import MetaParser
from pocketpaw.money import normalize_currency, to_minor
from pocketpaw.paw_bar.catalog_store import DEFAULT_CATALOG_MAX_ITEMS, catalog_max_items
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.foreign_grounding import GROUNDING_USER_AGENT, crawlable_origin
from pocketpaw_ee.sites.safe_fetch import (
    PER_FETCH_TIMEOUT_SEC,
    FetchBudgetExceeded,
    FetchError,
    FetchResult,
    SafeFetcher,
)
from pocketpaw_ee.sites.url_crawler import POLITENESS_DELAY_SEC, allowed_by_robots, load_robots

logger = logging.getLogger(__name__)

IMPORT_MAX_ITEMS = DEFAULT_CATALOG_MAX_ITEMS  # the default; the live cap is config
IMPORT_MAX_PAGES = 30
# A full Shopify page with descriptions is ~1 MB, and 20 of them are allowed.
IMPORT_BYTE_CAP = 40 * 1024 * 1024
# Soft deadline: no fetch starts that could outlive it. Sized for platform
# paging (up to 20 Shopify / 50 Woo pages); a slower store returns a partial.
IMPORT_WALL_CLOCK_SEC = 60.0
IMPORT_BACKSTOP_GRACE_SEC = 5.0  # the hard asyncio.timeout sits this far past it

_FETCH_TIMEOUT_SEC = PER_FETCH_TIMEOUT_SEC
_DEADLINE_MARGIN_SEC = 1.0
_MAX_PRICE_MINOR = 10**12  # a price past this is a parse accident, not a product

_NAME_CHARS = 200
_DESCRIPTION_CHARS = 300
_URL_CHARS = 2048
_JSONLD_BLOCK_BYTES = 256 * 1024
_JSONLD_MAX_DEPTH = 12
_MAX_SITEMAPS = 4  # the index or urlset, plus up to three child sitemaps
_MAX_LINKS = 2000  # anchors / sitemap locs tracked per document
_SHOPIFY_PAGE_SIZE = 250
_SHOPIFY_MAX_PAGES = 20
_WOO_PAGE_SIZE = 100
_WOO_MAX_PAGES = 50

_PRODUCT_PATH_RE = re.compile(r"/(products?|shop|item|p)/", re.IGNORECASE)
_SHOPIFY_SHOP_RE = re.compile(r"Shopify\.shop\s*=")
_SHOPIFY_ACTIVE_CURRENCY_RE = re.compile(
    r'Shopify\.currency\s*=\s*\{[^}]*?"active"\s*:\s*"([A-Za-z]{3})"'
)
_CURRENCY_CODE_RE = re.compile(r'"currencyCode"\s*:\s*"([A-Za-z]{3})"')
_WOO_BODY_CLASS_RE = re.compile(r"<body[^>]*class=[\"'][^\"']*\bwoocommerce\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_DECIMAL_COMMA_RE = re.compile(r"\d*,\d{1,2}")
_XML_ENCODING_RE = re.compile(r"""\s*<\?xml\b[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")

Platform = Literal["shopify", "woocommerce", ""]
Source = Literal["shopify", "woocommerce", "jsonld", "opengraph", "csv", ""]


class ImportedProduct(BaseModel):
    """One product found on the store's site, already normalised to catalog shape."""

    id: str  # "shopify:<id>" | "woo:<id>" | "web:<sha1(path)[:16]>"
    name: str
    price_cents: int = Field(ge=0)  # ISO 4217 minor units of currency (2 places if unknown)
    currency: str = ""  # ISO 4217, upper-case; "" when unknown
    image_url: str = ""  # https only
    url: str = ""  # site path on the read host (a CSV may give an absolute url), else ""
    description: str = ""
    in_stock: bool | None = None


class CatalogImportPreview(BaseModel):
    """What an import would add. ``reason`` is set only when ``status`` is failed:
    origin_missing | origin_unverified | origin_verification_stale |
    site_not_deployed | blocked_by_robots | timeout | fetch_failed, or a CSV's
    csv_empty | csv_unreadable | csv_no_name_column | csv_no_price_column."""

    status: Literal["ok", "partial", "empty", "failed"]
    reason: str = ""
    source: Source = ""
    host: str = ""
    items: list[ImportedProduct] = Field(default_factory=list)
    total_found: int = 0
    warnings: list[str] = Field(default_factory=list)


@dataclass
class ParsedProducts:
    """A parser's output: normalised products, and how many had no usable price."""

    items: list[ImportedProduct] = field(default_factory=list)
    skipped_no_price: int = 0
    skipped_bad_price: int = 0  # a price that overflows or passes _MAX_PRICE_MINOR
    currency: str = ""  # the first priceCurrency seen (JSON-LD), for Shopify's fallback

    def extend(self, other: ParsedProducts) -> None:
        self.items.extend(other.items)
        self.skipped_no_price += other.skipped_no_price
        self.skipped_bad_price += other.skipped_bad_price
        self.currency = self.currency or other.currency


@dataclass
class _Raw:
    """A product as a reader found it, before ``_normalise``."""

    id: str
    name: Any
    price: Decimal | None
    currency: Any = ""
    image: Any = ""
    url: Any = ""
    description: Any = ""
    in_stock: bool | None = None


# --------------------------------------------------------------------------- #
# Normalisation (every reader goes through here)
# --------------------------------------------------------------------------- #


class _TextOnly(HTMLParser):
    """Visible text of an HTML fragment; entities decoded, script/style dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "li", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _clean_text(value: Any, limit: int) -> str:
    """Plain, single-line text capped at ``limit`` chars (ellipsis on a cut)."""
    if not isinstance(value, str) or not value:
        return ""
    parser = _TextOnly()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 — a hostile fragment degrades to its raw text
        text = value
    # Whitespace first: a newline is a control character, and dropping it before
    # folding would glue the words on either side together.
    text = _WHITESPACE_RE.sub(" ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _price(value: Any) -> Decimal | None:
    """A non-negative decimal amount from a number or a string. A lone comma
    followed by one or two digits (and no dot) is a decimal comma ("19,99");
    any other comma is a thousands separator ("1,500", "1,299.00")."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if _DECIMAL_COMMA_RE.fullmatch(text):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def _absolute(value: Any, base: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if raw.startswith("//"):
        return "https:" + raw
    return urljoin(base, raw) if base else raw


def _image_url(value: Any, base: str) -> str:
    url = _absolute(value, base)
    if not url.lower().startswith("https://") or len(url) > _URL_CHARS:
        return ""
    return url


def _site_path(value: Any, base: str, host: str) -> str:
    """The on-host path of ``value`` (a single leading slash), or ""."""
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        absolute = urljoin(f"https://{host}/", value) if host else ""
    else:
        absolute = _absolute(value, base)
    if not absolute:
        return ""
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") != host:
        return ""
    path = "/" + (parts.path or "/").lstrip("/")
    return path if len(path) <= _URL_CHARS else ""


def _web_id(path: str, name: Any) -> str:
    """Stable id for a product read off a page: its url-or-page path AND its name,
    so url-less products on one listing page stay distinct across re-imports."""
    key = (path.rstrip("/").lower() or "/") + "\n" + _clean_text(name, _NAME_CHARS).lower()
    return "web:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]  # noqa: S324 — an id, not a secret


def _normalise(
    raws: Iterable[_Raw], *, base: str = "", host: str = "", keep_absolute_urls: bool = False
) -> ParsedProducts:
    """Raw reader output → catalog-shaped products. A product with no usable
    price or name is dropped (a card with no price is worse than no card), and so
    is one whose price overflows or passes ``_MAX_PRICE_MINOR``: one absurd
    value costs that product, never the import. ``keep_absolute_urls`` (CSV,
    which has no host to check against) keeps an http(s) product url as given
    instead of dropping it."""
    out = ParsedProducts()
    for raw in raws:
        name = _clean_text(raw.name, _NAME_CHARS)
        if raw.price is None or not name:
            out.skipped_no_price += 1
            continue
        try:
            currency = normalize_currency(raw.currency)
        except ValueError:
            currency = ""
        try:
            cents = to_minor(raw.price, currency)
        except (ArithmeticError, ValueError):
            cents = -1
        if not 0 <= cents <= _MAX_PRICE_MINOR:
            out.skipped_bad_price += 1
            continue
        url = _site_path(raw.url, base, host) if host else ""
        if not url and isinstance(raw.url, str) and raw.url.startswith("/"):
            url = "/" + raw.url.lstrip("/")
        elif not url and keep_absolute_urls and isinstance(raw.url, str):
            absolute = raw.url.strip()
            if absolute.lower().startswith(("http://", "https://")):
                url = absolute
        product_id = raw.id or _web_id(url or urlsplit(base).path or "/", name)
        out.items.append(
            ImportedProduct(
                id=product_id,
                name=name,
                price_cents=cents,
                currency=currency,
                image_url=_image_url(raw.image, base),
                url=url if len(url) <= _URL_CHARS else "",
                description=_clean_text(raw.description, _DESCRIPTION_CHARS),
                in_stock=raw.in_stock,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #


def detect_platform(html: str | bytes, headers: Mapping[str, str] | None = None) -> Platform:
    """Shopify / WooCommerce from the homepage, or "". A hint only: each reader
    confirms it by getting valid JSON back."""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html or ""
    head = {k.lower(): v for k, v in (headers or {}).items()}
    if (
        "x-shopid" in head
        or "x-shopify-stage" in head
        or "shopify" in head.get("powered-by", "").lower()
        or "cdn.shopify.com" in text
        or _SHOPIFY_SHOP_RE.search(text)
    ):
        return "shopify"
    if "/wp-content/plugins/woocommerce/" in text or (
        "api.w.org" in head.get("link", "") and _WOO_BODY_CLASS_RE.search(text)
    ):
        return "woocommerce"
    return ""


def shopify_currency(html: str) -> str:
    """The store's currency from its homepage: ``Shopify.currency.active``, then
    a ``"currencyCode"`` marker. "" when neither is there."""
    for pattern in (_SHOPIFY_ACTIVE_CURRENCY_RE, _CURRENCY_CODE_RE):
        match = pattern.search(html or "")
        if match:
            return match.group(1).upper()
    return ""


def parse_shopify_products(payload: Any, currency: str) -> ParsedProducts:
    """``/products.json`` → products. Price is the lowest AVAILABLE variant's
    (the lowest of all when none is available); in stock when any variant is."""
    products = payload.get("products") if isinstance(payload, dict) else None
    raws: list[_Raw] = []
    for product in products if isinstance(products, list) else []:
        if not isinstance(product, dict) or product.get("id") in (None, ""):
            continue
        variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
        flags = [v["available"] for v in variants if isinstance(v.get("available"), bool)]
        available = [v for v in variants if v.get("available") is True]
        priced = (_price(v.get("price")) for v in available or variants)
        prices = [p for p in priced if p is not None]
        images = product.get("images") or []
        image = images[0].get("src", "") if images and isinstance(images[0], dict) else ""
        handle = product.get("handle")
        raws.append(
            _Raw(
                id=f"shopify:{product['id']}",
                name=product.get("title"),
                price=min(prices) if prices else None,
                currency=currency,
                image=image,
                url=f"/products/{handle}" if isinstance(handle, str) and handle else "",
                description=product.get("body_html"),
                in_stock=any(flags) if flags else None,
            )
        )
    return _normalise(raws)


def parse_woo_products(payload: Any, host: str = "") -> ParsedProducts:
    """WooCommerce Store API ``/wp-json/wc/store/v1/products`` → products.

    ``prices.price`` is in the store's own minor units (``currency_minor_unit``
    places, which a shop can configure). It is turned into the major amount here
    and ``_normalise`` re-expresses it in ISO 4217 minor units, so the shop's
    setting never leaks into the catalog."""
    raws: list[_Raw] = []
    for product in payload if isinstance(payload, list) else []:
        if not isinstance(product, dict) or product.get("id") in (None, ""):
            continue
        prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
        amount: Decimal | None = None
        minor = prices.get("currency_minor_unit", 2)
        raw_price = prices.get("price")
        if isinstance(minor, int) and 0 <= minor <= 6 and isinstance(raw_price, str | int):
            try:
                amount = Decimal(int(raw_price)) / (Decimal(10) ** minor)
            except (TypeError, ValueError):
                amount = None
        if amount is not None and amount < 0:
            amount = None
        images = product.get("images") or []
        image = images[0].get("src", "") if images and isinstance(images[0], dict) else ""
        stock = product.get("is_in_stock")
        raws.append(
            _Raw(
                id=f"woo:{product['id']}",
                name=product.get("name"),
                price=amount,
                currency=prices.get("currency_code", ""),
                image=image,
                url=product.get("permalink", ""),
                description=product.get("short_description") or product.get("description"),
                in_stock=stock if isinstance(stock, bool) else None,
            )
        )
    return _normalise(raws, base=f"https://{host}/" if host else "", host=host)


class _PageScan(MetaParser):
    """``MetaParser``'s meta tags, plus JSON-LD blocks and same-page anchors."""

    def __init__(self) -> None:
        super().__init__()
        self.jsonld: list[str] = []
        self.links: list[str] = []
        self._block: list[str] | None = None
        self._block_len = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and attr.get("type", "").strip().lower() == "application/ld+json":
            self._block, self._block_len = [], 0
        elif tag == "a" and attr.get("href") and len(self.links) < _MAX_LINKS:
            self.links.append(attr["href"].strip())

    def handle_data(self, data: str) -> None:
        if self._block is not None:
            self._block_len += len(data)
            self._block.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._block is not None:
            if self._block_len <= _JSONLD_BLOCK_BYTES:
                self.jsonld.append("".join(self._block))
            self._block = None


def _scan(html: str | bytes) -> _PageScan:
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html or ""
    scan = _PageScan()
    try:
        scan.feed(text)
        scan.close()
    except Exception:  # noqa: BLE001 — keep whatever was read before the parser gave up
        logger.debug("catalog_import: html parse stopped early", exc_info=True)
    return scan


def _is_product(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.rsplit("/", 1)[-1] == "Product" for k in kinds)


def _product_nodes(node: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    """Every schema.org Product in a JSON-LD value: through lists, ``@graph`` and
    any nesting (ItemList, WebPage.mainEntity, ProductGroup.hasVariant)."""
    if depth > _JSONLD_MAX_DEPTH:
        return
    if isinstance(node, list):
        for child in node:
            yield from _product_nodes(child, depth + 1)
    elif isinstance(node, dict):
        if _is_product(node):
            yield node
            return
        for value in node.values():
            if isinstance(value, dict | list):
                yield from _product_nodes(value, depth + 1)


def _first_str(value: Any, *keys: str) -> str:
    """A string, the first string of a list, or the first of ``keys`` on a dict."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), str):
                return value[key]
        return ""
    return value if isinstance(value, str) else ""


def _availability(value: Any) -> bool | None:
    text = _first_str(value, "@id").strip().lower()
    if text.endswith("outofstock") or text in ("oos", "out of stock", "soldout", "sold out"):
        return False
    if text.endswith("instock") or text == "in stock":
        return True
    return None


def _offer_summary(offers: Any) -> tuple[Decimal | None, str, bool | None]:
    """Lowest price, its currency, and stock across an Offer, a list of them, or
    an AggregateOffer (``lowPrice``)."""
    entries = offers if isinstance(offers, list) else [offers]
    best: tuple[Decimal, str] | None = None
    stock: list[bool | None] = []
    for offer in entries:
        if not isinstance(offer, dict):
            continue
        spec = offer.get("priceSpecification")
        spec = spec[0] if isinstance(spec, list) and spec else spec
        candidates = [offer.get("price"), offer.get("lowPrice")]
        if isinstance(spec, dict):
            candidates.append(spec.get("price"))
        amount = next((p for p in map(_price, candidates) if p is not None), None)
        currency = offer.get("priceCurrency") or (
            spec.get("priceCurrency") if isinstance(spec, dict) else ""
        )
        if amount is not None and (best is None or amount < best[0]):
            best = (amount, currency if isinstance(currency, str) else "")
        stock.append(_availability(offer.get("availability")))
    in_stock: bool | None = None
    if True in stock:
        in_stock = True
    elif stock and all(s is False for s in stock):
        in_stock = False
    return (best[0], best[1], in_stock) if best else (None, "", in_stock)


def _jsonld_products(blocks: Iterable[str], page_url: str, host: str) -> ParsedProducts:
    raws: list[_Raw] = []
    currency = ""
    for block in blocks:
        try:
            data = json.loads(block)
        except (ValueError, RecursionError):
            continue  # one malformed block never costs the others
        for node in _product_nodes(data):
            price, node_currency, in_stock = _offer_summary(node.get("offers"))
            currency = currency or node_currency
            path = _site_path(_first_str(node.get("url")) or page_url, page_url, host)
            raws.append(
                _Raw(
                    id=_web_id(path or urlsplit(page_url).path or "/", node.get("name")),
                    name=node.get("name"),
                    price=price,
                    currency=node_currency,
                    image=_first_str(node.get("image"), "url", "contentUrl"),
                    url=path,
                    description=node.get("description"),
                    in_stock=in_stock,
                )
            )
    parsed = _normalise(raws, base=page_url, host=host)
    parsed.currency = currency.strip().upper() if isinstance(currency, str) else ""
    return parsed


def parse_jsonld_products(html: str | bytes, page_url: str, host: str) -> ParsedProducts:
    """Products from a page's ``<script type="application/ld+json">`` blocks."""
    return _jsonld_products(_scan(html).jsonld, page_url, host)


def _og_product(meta: Mapping[str, str], page_url: str, host: str) -> ParsedProducts:
    if not meta.get("og:type", "").strip().lower().startswith("product"):
        return ParsedProducts()

    def first(*keys: str) -> str:
        return next((meta[k] for k in keys if meta.get(k)), "")

    path = _site_path(first("og:url") or page_url, page_url, host)
    raw = _Raw(
        id=_web_id(path or urlsplit(page_url).path or "/", first("og:title")),
        name=first("og:title"),
        price=_price(first("product:price:amount", "og:price:amount")),
        currency=first("product:price:currency", "og:price:currency"),
        image=first("og:image:secure_url", "og:image"),
        url=path,
        description=first("og:description"),
        in_stock=_availability(first("product:availability", "og:availability")),
    )
    return _normalise([raw], base=page_url, host=host)


def parse_og_product(html: str | bytes, page_url: str, host: str) -> ParsedProducts:
    """One product from ``og:type=product`` + ``product:price:*`` (or ``og:price:*``)."""
    return _og_product(_scan(html).meta, page_url, host)


class _RefusedXML(Exception):
    """A sitemap declared a DTD or an entity: refused, nothing expanded."""


class _EnoughLocs(Exception):
    """``_MAX_LINKS`` locs read: stop parsing."""


def _refuse_dtd(*_args: Any) -> None:
    raise _RefusedXML


def _local(tag: str) -> str:
    return tag.rsplit(" ", 1)[-1].lower()


def _sitemap_locs(body: bytes) -> tuple[bool, list[str]]:
    """(is_index, <loc> values) of a sitemap; (False, []) when refused.

    Two locks against entity expansion. The body must be UTF-8 (sitemaps.org
    requires it; a UTF-8 BOM is allowed), carry no NUL (a BOM-less UTF-16 body
    is valid UTF-8 byte-wise, its NULs give it away) and declare no other
    encoding; the decoded text is then fed to expat as a str, so expat cannot
    re-detect an encoding. And the expat parser raises on any DOCTYPE, entity
    declaration or external entity reference, so nothing is ever expanded."""
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
    locs: list[str] = []
    current: list[str] | None = None

    def start(tag: str, _attrs: Any) -> None:
        nonlocal current
        if not root:
            root.append(_local(tag))
        if _local(tag) == "loc":
            current = []

    def data(chunk: str) -> None:
        if current is not None:
            current.append(chunk)

    def end(tag: str) -> None:
        nonlocal current
        if current is not None and _local(tag) == "loc":
            loc = "".join(current).strip()
            current = None
            if loc:
                locs.append(loc)
                if len(locs) >= _MAX_LINKS:
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


def _prefer_products(urls: list[str]) -> list[str]:
    """URLs whose path looks like a product's, or all of them when none does."""
    preferred = [u for u in urls if _PRODUCT_PATH_RE.search(urlsplit(u).path + "/")]
    return preferred or urls


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class _DeadlineReached(Exception):
    """The next fetch could outlive the soft deadline: stop and keep what we have."""


class _Run:
    """One import against one verified host: a fetcher, robots, and counters."""

    def __init__(self, fetcher: SafeFetcher, host: str, delay: float, deadline: float) -> None:
        self.fetcher = fetcher
        self.host = host
        self.base = f"https://{host}/"
        self.delay = delay
        self.deadline = deadline  # a time.monotonic() value
        self.deadline_hit = False
        self.max_items = catalog_max_items()
        self.robots: Any = None
        self.requests = 0
        self.pages_failed = 0
        self.skipped_by_robots = 0
        self.budget_hit = False
        self.warnings: list[str] = []

    def url(self, target: str) -> str:
        return urljoin(self.base, target)

    def allowed(self, url: str) -> bool:
        return allowed_by_robots(self.robots, url, GROUNDING_USER_AGENT)

    def _check_deadline(self) -> None:
        if self.deadline - time.monotonic() < _FETCH_TIMEOUT_SEC + _DEADLINE_MARGIN_SEC:
            self.deadline_hit = True
            raise _DeadlineReached

    async def get(self, target: str) -> FetchResult | None:
        """GET an on-host url, robots first. None when blocked or failed; a
        crossed byte budget or the soft deadline propagates so the caller
        stops walking."""
        url = self.url(target)
        if (urlsplit(url).hostname or "") != self.host:
            return None
        if not self.allowed(url):
            self.skipped_by_robots += 1
            return None
        self._check_deadline()
        if self.requests and self.delay:
            await asyncio.sleep(self.delay)
            self._check_deadline()
        self.requests += 1
        try:
            return await self.fetcher.fetch(url, allowed_host=self.host)
        except FetchBudgetExceeded:
            raise
        except (FetchError, ValidationError, httpx.HTTPError) as exc:
            logger.info("catalog_import: %s not read (%s)", url, getattr(exc, "code", type(exc)))
            return None

    async def get_json(self, target: str) -> Any:
        result = await self.get(target)
        if result is None or result.status != 200:
            return None
        try:
            return json.loads(result.body)
        except (ValueError, RecursionError):
            return None

    async def pages(self, path: str, size: int, max_pages: int, key: str | None) -> list | None:
        """Every item of a paged JSON list endpoint (``path`` ends ``page=``),
        until a short page, ``max_pages`` or the item cap. None when page 1 is not
        the expected JSON (the reader falls back); on page 1 a crossed byte budget
        or the soft deadline propagates, and on a later page either one (or a bad
        page) ends the paging with what was read."""
        items: list = []
        for page in range(1, max_pages + 1):
            try:
                payload = await self.get_json(f"{path}{page}")
            except FetchBudgetExceeded:
                if page == 1:
                    raise
                self.budget_hit = True
                break
            except _DeadlineReached:
                if page == 1:
                    raise
                break  # deadline_hit is set; the pages read so far stand
            batch = payload.get(key) if key and isinstance(payload, dict) else payload
            if not isinstance(batch, list):
                if page == 1:
                    return None
                break
            items.extend(batch)
            if len(batch) < size or len(items) >= self.max_items:
                break
        return items

    async def shopify(self, home_html: str) -> ParsedProducts | None:
        products = await self.pages(
            f"/products.json?limit={_SHOPIFY_PAGE_SIZE}&page=",
            _SHOPIFY_PAGE_SIZE,
            _SHOPIFY_MAX_PAGES,
            "products",
        )
        if products is None:
            return None
        payload = {"products": products}
        currency = shopify_currency(home_html)
        if not currency:
            handle = next(
                (
                    p.get("handle")
                    for p in payload["products"]
                    if isinstance(p, dict) and isinstance(p.get("handle"), str)
                ),
                None,
            )
            if handle:
                try:
                    page = await self.get(f"/products/{handle}")
                except _DeadlineReached:
                    page = None  # keep the products; the currency stays unknown
                if page is not None and page.status == 200:
                    currency = parse_jsonld_products(page.body, page.url, self.host).currency
        if not currency:
            self.warnings.append("currency_unknown")
        return parse_shopify_products(payload, currency)

    async def woo(self) -> ParsedProducts | None:
        products = await self.pages(
            f"/wp-json/wc/store/v1/products?per_page={_WOO_PAGE_SIZE}&page=",
            _WOO_PAGE_SIZE,
            _WOO_MAX_PAGES,
            None,
        )
        return None if products is None else parse_woo_products(products, self.host)

    async def product_pages(self, home: _PageScan) -> list[str]:
        """Candidate product pages: from the sitemap(s) robots.txt names (else
        /sitemap.xml, one index level deep), else the homepage's links."""
        declared = list(self.robots.site_maps() or []) if self.robots is not None else []
        queue = [u for u in declared if (urlsplit(u).hostname or "") == self.host]
        queue = queue or [self.url("/sitemap.xml")]
        pages: list[str] = []
        fetched = 0
        while queue and fetched < _MAX_SITEMAPS and len(pages) < _MAX_LINKS:
            sitemap = queue.pop(0)
            fetched += 1
            result = await self.get(sitemap)
            if result is None or result.status != 200:
                continue
            is_index, locs = _sitemap_locs(result.body)
            on_host = [u for u in locs if (urlsplit(u).hostname or "") == self.host]
            if is_index:
                if fetched == 1:  # one level of index only
                    queue = _prefer_products(on_host)[: _MAX_SITEMAPS - 1]
                continue
            pages.extend(on_host)
        if not pages:
            pages = [
                u
                for u in (self.url(h) for h in home.links)
                if (urlsplit(u).hostname or "") == self.host and _PRODUCT_PATH_RE.search(u)
            ]
        seen: set[str] = {self.base}
        unique: list[str] = []
        for page in _prefer_products(pages):
            key = page.split("#", 1)[0]
            if key not in seen:
                seen.add(key)
                unique.append(key)
        return unique[:IMPORT_MAX_PAGES]

    async def generic(self, home: _PageScan, home_url: str) -> tuple[ParsedProducts, Source]:
        """JSON-LD first, OpenGraph per page, starting with the homepage."""
        found = ParsedProducts()
        jsonld = og = 0

        def take(blocks: list[str], meta: Mapping[str, str], page_url: str) -> None:
            nonlocal jsonld, og
            parsed = _jsonld_products(blocks, page_url, self.host)
            if parsed.items:
                jsonld += len(parsed.items)
            else:
                parsed = _og_product(meta, page_url, self.host)
                og += len(parsed.items)
            found.extend(parsed)

        take(home.jsonld, home.meta, home_url)
        try:
            for page_url in await self.product_pages(home):
                result = await self.get(page_url)
                if result is None:
                    if self.allowed(page_url):
                        self.pages_failed += 1
                    continue
                if result.status != 200 or "html" not in (result.content_type or "html"):
                    self.pages_failed += 1
                    continue
                scan = _scan(result.body)
                take(scan.jsonld, scan.meta, result.url)
        except FetchBudgetExceeded:
            self.budget_hit = True
        except _DeadlineReached:
            pass  # deadline_hit is set; the pages read so far stand
        source: Source = "jsonld" if jsonld else ("opengraph" if og else "")
        return found, source


def _rank(item: ImportedProduct) -> int:
    return {True: 0, None: 1, False: 2}[item.in_stock]


def _failed(reason: str, host: str = "") -> CatalogImportPreview:
    return CatalogImportPreview(status="failed", reason=reason, host=host)


async def _preview(run: _Run, *, platforms: bool = True) -> CatalogImportPreview:
    run.robots, robots_warning = await load_robots(
        run.fetcher, urlsplit(run.base), allowed_host=run.host
    )
    if robots_warning:
        run.warnings.append("robots_unreadable")
    if not run.allowed(run.base):
        return _failed("blocked_by_robots", run.host)
    try:
        home = await run.get("/")
    except _DeadlineReached:
        return _failed("timeout", run.host)
    if home is None or home.status != 200:
        return _failed("fetch_failed", run.host)
    home_html = home.body.decode("utf-8", errors="replace")

    parsed: ParsedProducts | None = None
    source: Source = ""
    try:
        platform = detect_platform(home_html, home.headers) if platforms else ""
        if platform == "shopify":
            parsed, source = await run.shopify(home_html), "shopify"
        elif platform == "woocommerce":
            parsed, source = await run.woo(), "woocommerce"
    except FetchBudgetExceeded:
        run.budget_hit, parsed = True, None
    except _DeadlineReached:
        parsed = None
    if parsed is None or not parsed.items:
        if "currency_unknown" in run.warnings:
            run.warnings.remove("currency_unknown")
        parsed, source = await run.generic(_scan(home_html), home.url)

    unique: dict[str, ImportedProduct] = {}
    for item in parsed.items:
        unique.setdefault(item.id, item)
    items = sorted(unique.values(), key=_rank)  # stable: the store's own order within a rank

    warnings = list(run.warnings)
    if parsed.skipped_no_price:
        warnings.append(f"skipped_no_price:{parsed.skipped_no_price}")
    if parsed.skipped_bad_price:
        warnings.append(f"skipped_bad_price:{parsed.skipped_bad_price}")
    if run.skipped_by_robots:
        warnings.append(f"skipped_by_robots:{run.skipped_by_robots}")
    if run.pages_failed:
        warnings.append(f"pages_failed:{run.pages_failed}")
    if run.budget_hit:
        warnings.append("byte_budget_reached")
    if run.deadline_hit:
        warnings.append("deadline_reached")
    if not items:
        if run.deadline_hit:
            return _failed("timeout", run.host)
        status = "empty"
    elif run.pages_failed or run.budget_hit or run.deadline_hit:
        status = "partial"
    else:
        status = "ok"
    return CatalogImportPreview(
        status=status,
        source=source if items else "",
        host=run.host,
        items=items[: run.max_items],
        total_found=len(items),
        warnings=warnings,
    )


def hosted_host(site: Any) -> str:
    """Where a hosted Paw Site is served: its first live custom domain, else the
    bare host of its deployed ``url``; "" when it has neither."""
    for domain in getattr(site, "domains", None) or []:
        hostname = str(getattr(domain, "hostname", "") or "").strip().lower().rstrip(".")
        if getattr(domain, "status", "") == "live" and hostname:
            return hostname
    from pocketpaw_ee.paw_bar.embed import deployed_host

    return deployed_host(str(getattr(site, "url", "") or ""))


def _is_public_host(host: str) -> bool:
    """False for no host, a dotless or ``localhost`` name, or a non-global IP: a
    site in local mode, which the SSRF rules would refuse anyway."""
    if not host or "." not in host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        return True


async def preview_catalog_import(
    site: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    politeness_delay: float | None = None,
) -> CatalogImportPreview:
    """Read the products a site publishes. Writes nothing, never raises.

    A connected site is read on its verified origin with every reader; a hosted
    Paw Site on ``hosted_host(site)`` with the generic reader only.
    ``transport`` / ``resolver`` / ``politeness_delay`` are test seams, as on
    ``foreign_grounding.harvest_foreign_site``.
    """
    hosted = not getattr(site, "foreign_origin", False)
    if hosted:
        host = hosted_host(site)
        if not _is_public_host(host):
            return _failed("site_not_deployed")
    else:
        host, reason = await crawlable_origin(site)
        if reason:
            return _failed(reason)
    fetcher = SafeFetcher(
        total_byte_cap=IMPORT_BYTE_CAP,
        timeout_sec=_FETCH_TIMEOUT_SEC,
        user_agent=GROUNDING_USER_AGENT,
        transport=transport,
        resolver=resolver,
    )
    delay = POLITENESS_DELAY_SEC if politeness_delay is None else politeness_delay
    run = _Run(fetcher, host, delay, time.monotonic() + IMPORT_WALL_CLOCK_SEC)
    backstop = IMPORT_WALL_CLOCK_SEC + IMPORT_BACKSTOP_GRACE_SEC
    try:
        async with asyncio.timeout(backstop):
            return await _preview(run, platforms=not hosted)
    except TimeoutError:
        # Ahead of the catch-all: asyncio.timeout's TimeoutError is an Exception.
        # Only a fetch that overran its own timeout lands here; the soft
        # deadline normally returns first with what was read.
        logger.warning("catalog_import: %s exceeded %.0fs", host, backstop)
        return _failed("timeout", host)
    except FetchBudgetExceeded:
        return _failed("fetch_failed", host)
    except Exception:  # noqa: BLE001 — a raised import is an owner staring at a spinner
        logger.warning("catalog_import: preview of %s raised", host, exc_info=True)
        return _failed("fetch_failed", host)
    finally:
        await fetcher.aclose()


__all__ = [
    "IMPORT_BACKSTOP_GRACE_SEC",
    "IMPORT_BYTE_CAP",
    "IMPORT_MAX_ITEMS",
    "IMPORT_MAX_PAGES",
    "IMPORT_WALL_CLOCK_SEC",
    "CatalogImportPreview",
    "ImportedProduct",
    "ParsedProducts",
    "detect_platform",
    "hosted_host",
    "parse_jsonld_products",
    "parse_og_product",
    "parse_shopify_products",
    "parse_woo_products",
    "preview_catalog_import",
    "shopify_currency",
]
