# tests/cloud/test_paw_bar_catalog_import.py — the owner-facing half of the
# concierge catalog import.
#
#   * POST /paw-bar/admin/site/{id}/catalog/import/preview: the role gate (403),
#     tenancy (404 for another workspace's site) and a hosted site that was never
#     deployed answered ``site_not_deployed`` without a fetch. The reading itself
#     is covered in tests/ee/sites/test_catalog_import.py against a mocked origin.
#   * ``PawBarCatalogItem``'s new fields (description, in_stock) and its cleaning
#     validators: legacy bad values load cleaned, only id and price reject.
#   * The pass-through: a product card carries the item's url and description,
#     and a sold-out item is marked in the catalog the concierge prompt sees.

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from pocketpaw.paw_bar.models import PawBarCatalogItem, PawBarSpec
from tests.cloud.test_paw_bar_concierge_v2 import _site

_URL = "/paw-bar/admin/site/{sid}/catalog/import/preview"


def _build_app(role: str = "admin") -> FastAPI:
    from pocketpaw_ee.paw_bar.catalog_routes import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    app.include_router(router)
    override_workspace_role(app, role=role, workspace_id="ws-1")
    return app


@pytest_asyncio.fixture
async def owner(mongo_db):
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def member(mongo_db):
    transport = ASGITransport(app=_build_app("member"))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
def no_fetch(monkeypatch):
    """Fail the test if the route ever builds a fetcher."""
    from pocketpaw_ee.paw_bar import catalog_import

    def _refuse(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("the import fetched")

    monkeypatch.setattr(catalog_import, "SafeFetcher", _refuse)


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #


async def test_a_member_cannot_run_an_import(member, no_fetch):
    site = await _site(foreign_origin=True)
    resp = await member.post(_URL.format(sid=site.id), json={})
    assert resp.status_code == 403


async def test_another_workspaces_site_is_a_404(owner, no_fetch):
    site = await _site(workspace="ws-2", foreign_origin=True)
    resp = await owner.post(_URL.format(sid=site.id), json={})
    assert resp.status_code == 404
    malformed = await owner.post(_URL.format(sid="not-an-id"), json={})
    assert malformed.status_code == 404


async def test_an_undeployed_hosted_site_is_not_fetched(owner, no_fetch):
    site = await _site(url="http://localhost:8787")
    resp = await owner.post(_URL.format(sid=site.id), json={})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["status"], body["reason"], body["items"]) == ("failed", "site_not_deployed", [])


async def test_a_connected_site_without_a_verified_origin_fails_before_fetching(owner, no_fetch):
    site = await _site(foreign_origin=True)
    resp = await owner.post(_URL.format(sid=site.id), json={})
    assert resp.status_code == 200
    assert resp.json()["reason"] == "origin_unverified"


async def test_the_route_returns_the_preview_for_the_scoped_site(owner, monkeypatch):
    from pocketpaw_ee.paw_bar import catalog_import

    seen: list[Any] = []

    async def fake(site: Any, **_kw: Any) -> catalog_import.CatalogImportPreview:
        seen.append(site)
        return catalog_import.CatalogImportPreview(
            status="ok",
            source="shopify",
            host="brewco.com",
            items=[
                catalog_import.ImportedProduct(
                    id="shopify:1", name="Mug", price_cents=1200, currency="USD"
                )
            ],
            total_found=1,
        )

    monkeypatch.setattr(catalog_import, "preview_catalog_import", fake)
    site = await _site(foreign_origin=True)
    resp = await owner.post(_URL.format(sid=site.id), json={})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["id"] == "shopify:1"
    assert [str(s.id) for s in seen] == [str(site.id)]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


def test_catalog_items_default_the_new_fields():
    item = PawBarCatalogItem(id="mug", name="Mug")
    assert (item.description, item.in_stock) == ("", None)


@pytest.mark.parametrize(
    "url", ["", "/products/mug", "https://brewco.com/shop/mug", "http://brewco.com/m"]
)
def test_catalog_urls_that_are_accepted(url):
    assert PawBarCatalogItem(id="m", name="M", url=url).url == url


@pytest.mark.parametrize(
    ("fields", "field", "cleaned"),
    [
        ({"name": "  " + "x" * 900}, "name", "x" * 200),
        ({"description": "x" * 301}, "description", "x" * 300),
        ({"image_url": "javascript:alert(1)"}, "image_url", ""),
        ({"image_url": "data:image/png;base64,AAAA"}, "image_url", ""),
        ({"image_url": "/relative.jpg"}, "image_url", ""),
        ({"image_url": "https://a/" + "x" * 2048}, "image_url", ""),
        ({"url": "//evil.example/mug"}, "url", ""),
        ({"url": "javascript:alert(1)"}, "url", ""),
        ({"url": "products/mug"}, "url", ""),
        ({"url": "/" + "x" * 2048}, "url", ""),
        ({"currency": "USDT"}, "currency", "USD"),
        ({"currency": ""}, "currency", "USD"),
    ],
)
def test_legacy_bad_values_load_cleaned_instead_of_raising(fields, field, cleaned):
    fields = {"name": "M", **fields}  # copy: parametrize dicts are shared across runs
    item = PawBarCatalogItem(id="m", **fields)
    assert getattr(item, field) == cleaned


@pytest.mark.parametrize("fields", [{"id": "  "}, {"price_cents": -1}])
def test_the_id_and_price_rules_still_reject(fields):
    with pytest.raises(ValidationError):
        PawBarCatalogItem(**{"id": "m", "name": "M", **fields})


def test_an_out_of_range_price_loads_as_zero_with_a_warning(caplog):
    from pocketpaw.paw_bar.models import MAX_CATALOG_PRICE_MINOR

    at_cap = PawBarCatalogItem(id="a", name="A", price_cents=MAX_CATALOG_PRICE_MINOR)
    assert at_cap.price_cents == MAX_CATALOG_PRICE_MINOR
    with caplog.at_level("WARNING", logger="pocketpaw.paw_bar.models"):
        item = PawBarCatalogItem(id="b", name="B", price_cents=MAX_CATALOG_PRICE_MINOR + 1)
    assert item.price_cents == 0
    assert "reading it as 0" in caplog.text


def test_catalog_currency_is_upper_cased():
    assert PawBarCatalogItem(id="m", name="M", currency=" eur ").currency == "EUR"


def test_a_stored_spec_with_legacy_values_still_loads_cleaned():
    """Specs are re-validated from SQLite on every load: one written before the
    catalog rules existed must load, cleaned, not brick the widget."""
    raw = json.dumps(
        {
            "widget_id": "w",
            "pocket_id": "p",
            "catalog": [
                {
                    "id": "mug",
                    "name": "N" * 900,
                    "image_url": "data:image/png;base64,AAAA",
                    "currency": "usd ",
                    "price_cents": 1200,
                }
            ],
        }
    )
    [item] = PawBarSpec.model_validate_json(raw).catalog
    assert len(item.name) == 200
    assert item.image_url == ""
    assert item.currency == "USD"


# --------------------------------------------------------------------------- #
# Card + prompt pass-through
# --------------------------------------------------------------------------- #

_CATALOG = [
    PawBarCatalogItem(
        id="mug",
        name="Mug",
        price_cents=1200,
        url="/products/mug",
        description="Stoneware, 350 ml.",
        in_stock=True,
    ),
    PawBarCatalogItem(id="kettle", name="Kettle", price_cents=3000, in_stock=False),
    PawBarCatalogItem(id="filters", name="Filters", price_cents=500),
]


def test_a_product_card_carries_the_items_url_and_description():
    from pocketpaw_ee.paw_bar.card_spec import validate_and_hydrate

    spec = {"ui": {"type": "product-card", "props": {"ids": ["mug"]}}}
    out = validate_and_hydrate(spec, _CATALOG, verbs=("add_to_cart",))
    [item] = out["ui"]["props"]["items"]
    assert item["url"] == "/products/mug"
    assert item["description"] == "Stoneware, 350 ml."


def test_the_catalog_prompt_marks_only_sold_out_items():
    from pocketpaw_ee.paw_bar.concierge_runtime import _catalog_and_actions_block

    widget = SimpleNamespace(spec=PawBarSpec(widget_id="w", pocket_id="p"))
    block = _catalog_and_actions_block(widget, _CATALOG)
    lines = {line.split('"')[1]: line for line in block.splitlines() if 'id "' in line}
    assert lines["kettle"].endswith("(sold out)")
    assert "sold out" not in lines["mug"]
    assert "sold out" not in lines["filters"]  # unknown stock is not sold out
