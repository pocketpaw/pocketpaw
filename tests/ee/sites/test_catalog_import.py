# tests/ee/sites/test_catalog_import.py — the concierge catalog import
# (ee.pocketpaw_ee.paw_bar.catalog_import): reading a connected store's products
# off its own site for the owner to review.
#
# Two halves. The PURE parsers get payloads straight (Shopify /products.json, the
# WooCommerce Store API, JSON-LD, OpenGraph, platform detection, sitemaps). The
# ORCHESTRATOR runs against a mocked origin (httpx.MockTransport + a dict-backed
# resolver, the test_foreign_grounding harness): nothing opens a socket.
#
# The fetch gates are asserted as MECHANISMS, not codes: an unverified origin is
# proved un-fetched by ``seen == []``, a robots-disallowed endpoint by its path
# never appearing in ``seen``, an off-host redirect by the other host never being
# asked for. Fixture IPs are genuinely public (this Python treats RFC 5737 ranges
# as private).
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.paw_bar import catalog_import as ci

_WS = "ws-alpha"
_HOST = "shop.example"
_IP = "93.184.216.34"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _FakeSite:
    def __init__(self, **ov: Any) -> None:
        self.id = "site-foreign-1"
        self.workspace = _WS
        self.foreign_origin = True
        self.allowed_origins = [_HOST]
        self.__dict__.update(ov)


def _resolver(table: dict[str, list[str]] | None = None):
    source = table or {_HOST: [_IP], "evil.example": ["93.184.216.99"]}

    async def resolve(host: str) -> list[str]:
        if host not in source:
            raise OSError(f"no DNS for {host}")
        return source[host]

    return resolve


def _transport(routes: dict[str, Any], seen: list[httpx.Request]):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes.get(request.url.path)
        if callable(route):
            return await route(request)
        return route or httpx.Response(404, content=b"not found")

    return httpx.MockTransport(handler)


def _html(body: str, **headers: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html", **headers}, content=body)


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "application/json"}, content=json.dumps(payload)
    )


def _text(body: str, ctype: str = "text/plain") -> httpx.Response:
    return httpx.Response(200, headers={"content-type": ctype}, content=body)


async def _claim(*, verified_at: datetime | None = None) -> None:
    now = datetime.now(UTC)
    await SiteOriginClaim(
        workspace=_WS,
        host=_HOST,
        token="pawverify-token",
        status="verified",
        issued_at=now,
        expires_at=now + timedelta(days=7),
        issued_by="user-1",
        method="well-known",
        verified_at=now if verified_at is None else verified_at,
    ).insert()


async def _preview(routes: dict[str, Any], seen: list[httpx.Request] | None = None, site=None):
    return await ci.preview_catalog_import(
        site or _FakeSite(),
        transport=_transport(routes, seen if seen is not None else []),
        resolver=_resolver(),
        politeness_delay=0,
    )


def _paths(seen: list[httpx.Request]) -> list[str]:
    return [r.url.path for r in seen]


def _shopify_product(pid: int, *, available: bool = True, price: str = "12.50") -> dict:
    return {
        "id": pid,
        "title": f"Mug {pid}",
        "handle": f"mug-{pid}",
        "body_html": f"<p>Stoneware mug <b>{pid}</b></p>",
        "images": [{"src": f"//cdn.shopify.com/s/files/mug-{pid}.jpg"}],
        "variants": [{"price": price, "available": available}],
    }


_SHOPIFY_HOME = (
    '<html><head><script src="https://cdn.shopify.com/s/theme.js"></script>'
    '<script>Shopify.shop = "shop.myshopify.com"; '
    'Shopify.currency = {"active":"CAD","rate":"1.0"};</script></head>'
    "<body>Welcome</body></html>"
)
_SHOPIFY_ROBOTS = "User-agent: *\nDisallow: /cart\nDisallow: /checkout\n"


def _jsonld_page(name: str, price: str, *, currency: str = "EUR", path: str = "") -> str:
    node = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "image": {"@type": "ImageObject", "url": f"https://{_HOST}/img/{name}.jpg"},
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": currency,
            "availability": "https://schema.org/InStock",
        },
    }
    if path:
        node["url"] = f"https://{_HOST}{path}"
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(node)}</script>'
        f"</head><body><h1>{name}</h1></body></html>"
    )


# --------------------------------------------------------------------------- #
# Parsers: Shopify
# --------------------------------------------------------------------------- #


def test_shopify_takes_the_lowest_available_variant_and_cleans_every_field():
    payload = {
        "products": [
            {
                "id": 42,
                "title": "Pour-over &amp; kettle",
                "handle": "pour-over",
                "body_html": "<p>Gooseneck <script>x()</script>kettle.</p>",
                "images": [{"src": "//cdn.shopify.com/k.jpg"}],
                "variants": [
                    {"price": "30.00", "available": True},
                    {"price": "19.99", "available": True},
                    {"price": "5.00", "available": False},
                ],
            }
        ]
    }
    [item] = ci.parse_shopify_products(payload, "usd").items
    assert item.id == "shopify:42"
    assert item.name == "Pour-over & kettle"
    assert item.price_cents == 1999  # the cheapest AVAILABLE variant, not the sold-out 5.00
    assert item.currency == "USD"
    assert item.image_url == "https://cdn.shopify.com/k.jpg"
    assert item.url == "/products/pour-over"
    assert item.description == "Gooseneck kettle."
    assert item.in_stock is True


def test_a_sold_out_shopify_product_keeps_a_price_and_reads_out_of_stock():
    payload = {"products": [_shopify_product(1, available=False, price="8")]}
    payload["products"][0]["images"] = []
    [item] = ci.parse_shopify_products(payload, "").items
    assert item.in_stock is False
    assert item.price_cents == 800
    assert item.image_url == ""
    assert item.currency == ""


def test_a_shopify_product_with_no_price_is_skipped_and_counted():
    payload = {"products": [{"id": 1, "title": "Gift", "variants": [{"price": "n/a"}]}]}
    parsed = ci.parse_shopify_products(payload, "USD")
    assert parsed.items == []
    assert parsed.skipped_no_price == 1


def test_an_absurd_price_skips_that_product_and_keeps_the_rest():
    payload = {
        "products": [
            _shopify_product(1, price="12.50"),
            _shopify_product(2, price="1e30"),  # quantize overflows (InvalidOperation)
            _shopify_product(3, price="1" * 30),
            _shopify_product(4, price="20000000000"),  # 2 * 10**12 cents: past the cap
            _shopify_product(5, price="4"),
        ]
    }
    parsed = ci.parse_shopify_products(payload, "USD")
    assert [i.id for i in parsed.items] == ["shopify:1", "shopify:5"]
    assert parsed.skipped_bad_price == 3
    assert parsed.skipped_no_price == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19,99", "19.99"),  # decimal comma
        ("5,5", "5.5"),
        ("1,500", "1500"),  # thousands
        ("1,299.00", "1299.00"),
        ("12,345", "12345"),
        ("1,234,56", "123456"),  # two commas: not a decimal comma
        ("12.50", "12.50"),
    ],
)
def test_price_reads_a_decimal_comma_and_keeps_thousands_separators(raw, expected):
    from decimal import Decimal

    assert ci._price(raw) == Decimal(expected)


# --------------------------------------------------------------------------- #
# Parsers: WooCommerce
# --------------------------------------------------------------------------- #


def _woo(pid: int, price: str, minor: int, code: str, **ov: Any) -> dict:
    product = {
        "id": pid,
        "name": "Caf&eacute; beans &amp; filters",
        "permalink": f"https://{_HOST}/product/beans-{pid}/",
        "short_description": "<p>Single origin.</p>",
        "images": [{"src": f"https://{_HOST}/wp-content/uploads/b{pid}.jpg"}],
        "is_in_stock": True,
        "prices": {"price": price, "currency_code": code, "currency_minor_unit": minor},
    }
    product.update(ov)
    return product


def test_woo_converts_minor_units_to_hundredths_for_two_and_zero_decimal_currencies():
    parsed = ci.parse_woo_products(
        [_woo(1, "1999", 2, "USD"), _woo(2, "1500", 0, "JPY", is_in_stock=False)], _HOST
    )
    usd, jpy = parsed.items
    assert (usd.price_cents, usd.currency) == (1999, "USD")
    # Zero-decimal: ¥1500 stored as hundredths, the stack-wide convention.
    assert (jpy.price_cents, jpy.currency) == (150000, "JPY")
    assert usd.name == "Café beans & filters"
    assert usd.id == "woo:1"
    assert usd.url == "/product/beans-1/"
    assert usd.description == "Single origin."
    assert jpy.in_stock is False


def test_a_woo_permalink_off_the_verified_host_is_not_kept():
    [item] = ci.parse_woo_products(
        [_woo(1, "100", 2, "USD", permalink="https://elsewhere.example/p/1")], _HOST
    ).items
    assert item.url == ""


# --------------------------------------------------------------------------- #
# Parsers: JSON-LD and OpenGraph
# --------------------------------------------------------------------------- #

_PAGE = f"https://{_HOST}/products/kettle"


def _ld(*blocks: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body></body></html>"


def test_jsonld_reads_graph_list_roots_aggregate_offers_and_image_objects():
    graph = json.dumps(
        {
            "@graph": [
                {"@type": "WebSite", "name": "Shop"},
                {
                    "@type": ["Product", "Thing"],
                    "name": "Kettle",
                    "url": f"https://{_HOST}/products/kettle",
                    "image": {"@type": "ImageObject", "url": f"https://{_HOST}/k.jpg"},
                    "offers": {
                        "@type": "AggregateOffer",
                        "lowPrice": "24.5",
                        "priceCurrency": "gbp",
                        "availability": "http://schema.org/OutOfStock",
                    },
                },
            ]
        }
    )
    list_root = json.dumps(
        [
            {
                "@type": "Product",
                "name": "Filter papers",
                "url": "/products/filters",
                "image": ["https://cdn.example/f.jpg", "https://cdn.example/f2.jpg"],
                "offers": [
                    {"@type": "Offer", "price": 9, "priceCurrency": "GBP"},
                    {"@type": "Offer", "price": "7.25", "priceCurrency": "GBP"},
                ],
            }
        ]
    )
    parsed = ci.parse_jsonld_products(_ld(graph, "{not json", list_root), _PAGE, _HOST)
    kettle, filters = parsed.items
    assert (kettle.name, kettle.price_cents, kettle.currency) == ("Kettle", 2450, "GBP")
    assert kettle.image_url == f"https://{_HOST}/k.jpg"
    assert kettle.in_stock is False
    assert kettle.url == "/products/kettle"
    assert kettle.id.startswith("web:") and len(kettle.id) == 20
    assert filters.price_cents == 725  # the lowest of the offers
    assert filters.image_url == "https://cdn.example/f.jpg"
    assert filters.url == "/products/filters"
    assert parsed.currency == "GBP"


def test_url_less_products_on_one_listing_page_get_distinct_ids():
    graph = json.dumps(
        {
            "@graph": [
                {"@type": "Product", "name": "Mug", "offers": {"price": "12"}},
                {"@type": "Product", "name": "Kettle", "offers": {"price": "30"}},
            ]
        }
    )
    listing = f"https://{_HOST}/collections/all"
    mug, kettle = ci.parse_jsonld_products(_ld(graph), listing, _HOST).items
    assert mug.url == kettle.url == "/collections/all"
    assert mug.id != kettle.id
    # Stable on a re-import: the same page and name give the same id.
    again = ci.parse_jsonld_products(_ld(graph), listing, _HOST).items
    assert [i.id for i in again] == [mug.id, kettle.id]


def test_a_jsonld_id_is_stable_across_imports_and_falls_back_to_the_page_url():
    page = _ld(json.dumps({"@type": "Product", "name": "Kettle", "offers": {"price": "1"}}))
    first = ci.parse_jsonld_products(page, _PAGE, _HOST).items[0]
    again = ci.parse_jsonld_products(page, _PAGE + "/", _HOST).items[0]
    assert first.url == "/products/kettle"
    assert first.id == again.id


def test_jsonld_drops_a_non_https_image_and_an_offsite_url():
    node = {
        "@type": "Product",
        "name": "Mug",
        "url": "https://elsewhere.example/mug",
        "image": "http://insecure.example/m.jpg",
        "offers": {"price": "3"},
    }
    [item] = ci.parse_jsonld_products(_ld(json.dumps(node)), _PAGE, _HOST).items
    assert item.image_url == ""
    assert item.url == ""


def test_og_product_tags_are_read_when_there_is_no_jsonld():
    html = (
        '<html><head><meta property="og:type" content="product">'
        '<meta property="og:title" content="Grinder">'
        f'<meta property="og:image" content="https://{_HOST}/g.jpg">'
        '<meta property="product:price:amount" content="89.00">'
        '<meta property="product:price:currency" content="usd">'
        '<meta property="og:description" content="Burr grinder.">'
        "</head></html>"
    )
    [item] = ci.parse_og_product(html, f"https://{_HOST}/shop/grinder", _HOST).items
    assert (item.name, item.price_cents, item.currency) == ("Grinder", 8900, "USD")
    assert item.url == "/shop/grinder"
    assert item.description == "Burr grinder."
    assert (
        ci.parse_og_product('<meta property="og:type" content="website">', _PAGE, _HOST).items == []
    )


def test_names_and_descriptions_are_capped_and_stripped_of_control_characters():
    node = {
        "@type": "Product",
        "name": "A\x00b" + "x" * 400,
        "description": "word " * 200,
        "offers": {"price": "1"},
    }
    [item] = ci.parse_jsonld_products(_ld(json.dumps(node)), _PAGE, _HOST).items
    assert "\x00" not in item.name and len(item.name) == 200
    assert len(item.description) <= 300


# --------------------------------------------------------------------------- #
# Parsers: detection and sitemaps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("html", "headers", "expected"),
    [
        ("", {"X-ShopId": "123"}, "shopify"),
        ("", {"powered-by": "Shopify"}, "shopify"),
        ('<link href="https://cdn.shopify.com/a.css">', {}, "shopify"),
        ("<script>Shopify.shop = 'x';</script>", {}, "shopify"),
        ('<script src="/wp-content/plugins/woocommerce/a.js">', {}, "woocommerce"),
        (
            '<body class="home woocommerce-js woocommerce">',
            {"Link": '<https://s/wp-json/>; rel="https://api.w.org/"'},
            "woocommerce",
        ),
        ('<body class="woocommerce">', {}, ""),
        ("<html>plain</html>", {}, ""),
    ],
)
def test_detect_platform(html, headers, expected):
    assert ci.detect_platform(html, headers) == expected


def test_a_sitemap_with_a_dtd_is_refused_before_parsing():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        b"<urlset><url><loc>&lol;</loc></url></urlset>"
    )
    assert ci._sitemap_locs(bomb) == (False, [])
    ok = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a/p/1?x=1&amp;y=2</loc></url></urlset>'
    assert ci._sitemap_locs(ok) == (False, ["https://a/p/1?x=1&y=2"])


_LAUGHS = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE lolz [<!ENTITY lol "lol">'
    + "".join(
        f'<!ENTITY lol{i} "{("&lol" + str(i - 1) + ";") * 10 if i > 1 else "&lol;" * 10}">'
        for i in range(1, 10)
    )
    + "]><urlset><url><loc>&lol9;</loc></url></urlset>"
)


@pytest.mark.parametrize(
    "body",
    [
        _LAUGHS.encode("utf-8"),
        _LAUGHS.encode("utf-16"),  # BOM: not UTF-8
        _LAUGHS.encode("utf-16-le"),  # no BOM: byte-valid UTF-8, its NULs give it away
        _LAUGHS.replace('version="1.0"', 'version="1.0" encoding="UTF-16"').encode("utf-16-be"),
    ],
    ids=["utf8", "utf16-bom", "utf16le-no-bom", "utf16be-declared"],
)
def test_an_entity_bomb_is_refused_in_any_encoding_without_expanding(body):
    started = time.monotonic()
    assert ci._sitemap_locs(body) == (False, [])
    assert time.monotonic() - started < 0.5


def test_a_sitemap_declaring_a_non_utf8_encoding_is_refused():
    body = (
        b'<?xml version="1.0" encoding="UTF-16"?>'
        b"<urlset><url><loc>https://a/p/1</loc></url></urlset>"
    )
    assert ci._sitemap_locs(body) == (False, [])


def test_the_parser_itself_refuses_a_doctype_with_no_entities():
    body = b'<!DOCTYPE urlset SYSTEM "https://evil.example/x.dtd"><urlset><url><loc>https://a/p/1</loc></url></urlset>'
    assert ci._sitemap_locs(body) == (False, [])


def test_a_utf8_bom_and_a_utf8_declaration_are_accepted():
    body = (
        b"\xef\xbb\xbf"
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://a/s1.xml</loc></sitemap></sitemapindex>"
    )
    assert ci._sitemap_locs(body) == (True, ["https://a/s1.xml"])


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


async def test_shopify_happy_path(beanie_test_db):
    await _claim()
    products = [
        _shopify_product(1, available=False),
        _shopify_product(2),
        _shopify_product(3, price="4"),
    ]
    seen: list[httpx.Request] = []
    preview = await _preview(
        {
            "/robots.txt": _text(_SHOPIFY_ROBOTS),
            "/": _html(_SHOPIFY_HOME),
            "/products.json": _json({"products": products}),
        },
        seen,
    )
    assert (preview.status, preview.reason, preview.source) == ("ok", "", "shopify")
    assert preview.host == _HOST
    # In-stock first, then the store's own order; currency from Shopify.currency.
    assert [i.id for i in preview.items] == ["shopify:2", "shopify:3", "shopify:1"]
    assert {i.currency for i in preview.items} == {"CAD"}
    assert preview.items[0].url == "/products/mug-2"
    assert preview.total_found == 3
    assert "/cart.js" not in _paths(seen)
    assert all(r.headers["user-agent"].startswith("PawSitesConcierge/") for r in seen)


async def test_shopify_currency_falls_back_to_the_first_product_pages_jsonld(beanie_test_db):
    await _claim()
    home = '<html><script src="https://cdn.shopify.com/t.js"></script></html>'
    preview = await _preview(
        {
            "/": _html(home),
            "/products.json": _json({"products": [_shopify_product(7)]}),
            "/products/mug-7": _html(_jsonld_page("Mug", "12.50", currency="AUD")),
        }
    )
    assert preview.items[0].currency == "AUD"
    assert "currency_unknown" not in preview.warnings


async def test_shopify_404_falls_back_to_jsonld_from_the_sitemap(beanie_test_db):
    await _claim()
    sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>https://{_HOST}/products/kettle</loc></url>"
        f"<url><loc>https://{_HOST}/about</loc></url>"
        f"<url><loc>https://other.example/products/x</loc></url></urlset>"
    )
    seen: list[httpx.Request] = []
    preview = await _preview(
        {
            "/robots.txt": _text(f"User-agent: *\nSitemap: https://{_HOST}/sitemap.xml\n"),
            "/": _html(_SHOPIFY_HOME),
            "/products.json": httpx.Response(404),
            "/sitemap.xml": _text(sitemap, "application/xml"),
            "/products/kettle": _html(_jsonld_page("Kettle", "30", path="/products/kettle")),
        },
        seen,
    )
    assert (preview.status, preview.source) == ("ok", "jsonld")
    [item] = preview.items
    assert (item.name, item.price_cents, item.currency) == ("Kettle", 3000, "EUR")
    assert "/about" not in _paths(seen)  # product-looking paths are preferred


async def test_woocommerce_happy_path(beanie_test_db):
    await _claim()
    home = '<html><link rel="stylesheet" href="/wp-content/plugins/woocommerce/x.css"></html>'
    preview = await _preview(
        {
            "/": _html(home),
            "/wp-json/wc/store/v1/products": _json([_woo(5, "2500", 2, "USD")]),
        }
    )
    assert (preview.status, preview.source) == ("ok", "woocommerce")
    assert preview.items[0].id == "woo:5"
    assert preview.items[0].price_cents == 2500


async def test_robots_blocking_the_platform_endpoint_falls_back_without_asking_it(
    beanie_test_db,
):
    await _claim()
    seen: list[httpx.Request] = []
    preview = await _preview(
        {
            "/robots.txt": _text("User-agent: *\nDisallow: /products.json\n"),
            "/": _html(
                _SHOPIFY_HOME.replace(
                    "<body>Welcome</body>", '<body><a href="/products/kettle">K</a></body>'
                )
            ),
            "/products.json": _json({"products": [_shopify_product(1)]}),
            "/products/kettle": _html(_jsonld_page("Kettle", "30")),
        },
        seen,
    )
    assert "/products.json" not in _paths(seen)
    assert preview.source == "jsonld"
    assert [i.name for i in preview.items] == ["Kettle"]
    assert "skipped_by_robots:1" in preview.warnings


async def test_a_seed_blocked_by_robots_fails_with_its_own_reason(beanie_test_db):
    await _claim()
    seen: list[httpx.Request] = []
    preview = await _preview(
        {"/robots.txt": _text("User-agent: PawSitesConcierge\nDisallow: /\n")}, seen
    )
    assert (preview.status, preview.reason) == ("failed", "blocked_by_robots")
    assert _paths(seen) == ["/robots.txt"]


async def test_a_robots_redirect_off_the_host_is_not_followed_and_reads_as_unreadable(
    beanie_test_db,
):
    """Locked to the verified host: the off-host robots file (which would block
    everything) is never asked for. Unreadable robots is allow-all with a warning,
    the crawl's own policy."""
    await _claim()
    seen: list[httpx.Request] = []
    preview = await _preview(
        {
            "/robots.txt": httpx.Response(
                301, headers={"location": "https://evil.example/robots.txt"}
            ),
            "/": _html(_SHOPIFY_HOME),
            "/products.json": _json({"products": [_shopify_product(1)]}),
        },
        seen,
    )
    assert all(r.headers["host"] == _HOST for r in seen)
    assert "robots_unreadable" in preview.warnings
    assert (preview.status, preview.source) == ("ok", "shopify")


async def test_an_unverified_origin_is_never_fetched(beanie_test_db):
    seen: list[httpx.Request] = []
    preview = await _preview({"/": _html(_SHOPIFY_HOME)}, seen)
    assert (preview.status, preview.reason) == ("failed", "origin_unverified")
    assert seen == []


async def test_a_stale_origin_is_never_fetched(beanie_test_db):
    await _claim(verified_at=datetime.now(UTC) - timedelta(days=45))
    seen: list[httpx.Request] = []
    preview = await _preview({"/": _html(_SHOPIFY_HOME)}, seen)
    assert preview.reason == "origin_verification_stale"
    assert seen == []


async def test_a_hosted_site_is_not_connected_and_never_fetched(beanie_test_db):
    await _claim()
    seen: list[httpx.Request] = []
    preview = await _preview({}, seen, site=_FakeSite(foreign_origin=False))
    assert (preview.status, preview.reason) == ("failed", "not_connected_site")
    assert seen == []


async def test_an_offsite_redirect_is_refused(beanie_test_db):
    await _claim()
    seen: list[httpx.Request] = []
    preview = await _preview(
        {"/": httpx.Response(301, headers={"location": "https://evil.example/"})}, seen
    )
    assert (preview.status, preview.reason) == ("failed", "fetch_failed")
    assert all(r.headers["host"] == _HOST for r in seen)


async def test_the_backstop_ends_a_fetch_that_overruns_its_own_timeout(beanie_test_db, monkeypatch):
    await _claim()
    # MockTransport ignores httpx timeouts, so the trickle outlives the soft
    # deadline; the hard asyncio.timeout behind it ends the run.
    monkeypatch.setattr(ci, "IMPORT_WALL_CLOCK_SEC", 0.05)
    monkeypatch.setattr(ci, "IMPORT_BACKSTOP_GRACE_SEC", 0.05)
    monkeypatch.setattr(ci, "_FETCH_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(ci, "_DEADLINE_MARGIN_SEC", 0.0)

    async def trickle(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _html(_SHOPIFY_HOME)

    preview = await _preview({"/": trickle})
    assert (preview.status, preview.reason, preview.host) == ("failed", "timeout", _HOST)


async def test_the_soft_deadline_keeps_what_was_read_as_partial(beanie_test_db, monkeypatch):
    await _claim()
    # 1.0 s deadline, and no fetch starts with under 0.35 s left: with 0.2 s per
    # product page, about four of the ten pages are read before the stop.
    monkeypatch.setattr(ci, "IMPORT_WALL_CLOCK_SEC", 1.0)
    monkeypatch.setattr(ci, "_FETCH_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(ci, "_DEADLINE_MARGIN_SEC", 0.05)
    names = [f"Mug{i}" for i in range(10)]

    def slow(name: str):
        async def page(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.2)
            return _html(_jsonld_page(name, "10"))

        return page

    home = "<html><body>" + "".join(f'<a href="/products/{n}">{n}</a>' for n in names)
    routes: dict[str, Any] = {"/": _html(home + "</body></html>")}
    routes.update({f"/products/{n}": slow(n) for n in names})
    seen: list[httpx.Request] = []
    started = time.monotonic()
    preview = await _preview(routes, seen)
    elapsed = time.monotonic() - started

    assert (preview.status, preview.reason) == ("partial", "")
    assert "deadline_reached" in preview.warnings
    assert 1 <= len(preview.items) < len(names)
    assert [i.name for i in preview.items] == names[: len(preview.items)]
    assert elapsed < 1.0  # stopped on its own, well before the backstop
    product_paths = [p for p in _paths(seen) if p.startswith("/products/")]
    assert len(product_paths) == len(preview.items)  # no fetch was started and dropped


async def test_one_absurd_price_is_a_warning_not_a_failed_import(beanie_test_db):
    await _claim()
    products = [_shopify_product(1), _shopify_product(2, price="1e30"), _shopify_product(3)]
    preview = await _preview(
        {"/": _html(_SHOPIFY_HOME), "/products.json": _json({"products": products})}
    )
    assert (preview.status, preview.source) == ("ok", "shopify")
    assert [i.id for i in preview.items] == ["shopify:1", "shopify:3"]
    assert "skipped_bad_price:1" in preview.warnings


async def test_more_than_200_products_are_capped_with_the_total_reported(beanie_test_db):
    await _claim()
    products = [_shopify_product(i) for i in range(1, 231)]
    preview = await _preview(
        {"/": _html(_SHOPIFY_HOME), "/products.json": _json({"products": products})}
    )
    assert preview.total_found == 230
    assert len(preview.items) == ci.IMPORT_MAX_ITEMS == 200


async def test_a_store_with_no_products_is_empty_not_failed(beanie_test_db):
    await _claim()
    preview = await _preview({"/": _html("<html><body>Hello</body></html>")})
    assert (preview.status, preview.reason, preview.items) == ("empty", "", [])


async def test_unreadable_product_pages_make_the_preview_partial(beanie_test_db):
    await _claim()
    home = (
        '<html><body><a href="/products/kettle">K</a><a href="/products/broken">B</a></body></html>'
    )
    preview = await _preview(
        {
            "/": _html(home),
            "/products/kettle": _html(_jsonld_page("Kettle", "30")),
            "/products/broken": httpx.Response(500),
        }
    )
    assert preview.status == "partial"
    assert "pages_failed:1" in preview.warnings
    assert [i.name for i in preview.items] == ["Kettle"]
