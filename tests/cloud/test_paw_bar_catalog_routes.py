# tests/cloud/test_paw_bar_catalog_routes.py — the owner's catalog routes
# (``pocketpaw_ee.paw_bar.catalog_routes``) and the spec routes around them.
#
#   * GET/PUT/bulk/DELETE/reorder on /paw-bar/admin/site/{id}/catalog: role gates
#     (403), tenancy (404), a site with no widget (404 no_concierge_widget), the
#     wire shapes the dashboard codes against, 409 catalog_full with its limit,
#     and the 500-item bulk bound.
#   * The CSV preview route (multipart ``file``, 413 past 2 MB).
#   * The spec routes: 422 spec_too_large on both PATCHes and on rollback,
#     measured without the catalog; a PATCH body with a non-empty catalog adds its
#     items (never deletes), one without leaves the catalog; the old-client money
#     refusal (409 currency_units_client_outdated) runs first.
#   * Reads: the overview carries ``catalog_count`` and no catalog; the frozen
#     public spec endpoint fills ``spec.catalog`` from the store.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import MAX_SPEC_BYTES, PawBarBlock, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore
from tests.cloud.test_paw_bar_concierge_v2 import _site

_CAT = "/paw-bar/admin/site/{sid}/catalog"


def _widget(**ov: Any) -> PawBarWidget:
    d: dict[str, Any] = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        workspace_id="ws-1",
        allowed_domains=["brewco.com"],
        spec=PawBarSpec(widget_id="w", pocket_id="pocket-1"),
    )
    d.update(ov)
    return PawBarWidget(**d)


def _item(i: int | str, **ov: Any) -> dict:
    return {"id": f"p{i}", "name": f"Product {i}", "price_cents": 100 + int(str(i)[-1]), **ov}


def _app(role: str) -> FastAPI:
    from pocketpaw_ee.paw_bar.catalog_routes import router as catalog_router
    from pocketpaw_ee.paw_bar.router import router

    from tests.cloud.conftest import override_workspace_role

    app = FastAPI()
    app.include_router(router)
    app.include_router(catalog_router)
    override_workspace_role(app, role=role, workspace_id="ws-1")
    return app


@pytest_asyncio.fixture
async def rig(tmp_path, mongo_db):
    store = PawBarStore(tmp_path / "catalog_routes.db")
    clients: dict[str, AsyncClient] = {}
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        for role in ("admin", "member"):
            clients[role] = AsyncClient(
                transport=ASGITransport(app=_app(role)), base_url="http://t"
            )
        try:
            yield clients["admin"], clients["member"], store
        finally:
            for c in clients.values():
                await c.aclose()


async def _seeded(store: PawBarStore, n: int = 3):
    site = await _site()
    widget = await store.create_widget(_widget())
    if n:
        await store.upsert_catalog_items(widget.id, [_item(i) for i in range(n)])
    return site, widget


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


async def test_members_can_neither_read_nor_write(rig):
    _admin, member, store = rig
    site, _ = await _seeded(store)
    base = _CAT.format(sid=site.id)
    assert (await member.get(base)).status_code == 403
    assert (await member.put(f"{base}/items/p1", json=_item(1))).status_code == 403
    assert (await member.post(f"{base}/items:bulk", json={"items": []})).status_code == 403
    assert (await member.post(f"{base}/reorder", json={"ids": []})).status_code == 403
    assert (
        await member.request("DELETE", f"{base}/items", json={"ids": ["p1"]})
    ).status_code == 403


async def test_another_workspaces_site_and_a_site_without_a_widget_are_404(rig):
    admin, _member, store = rig
    await _seeded(store)
    foreign = await _site(workspace="ws-2")
    assert (await admin.get(_CAT.format(sid=foreign.id))).status_code == 404
    assert (await admin.get(_CAT.format(sid="nope"))).status_code == 404

    bare = await _site(pocket_id="pocket-without-widget")
    res = await admin.post(f"{_CAT.format(sid=bare.id)}/items:bulk", json={"items": [_item(1)]})
    assert (res.status_code, res.json()["detail"]) == (404, "no_concierge_widget")


# --------------------------------------------------------------------------- #
# Wire shapes
# --------------------------------------------------------------------------- #


async def test_list_pages_and_searches(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store, 0)
    await store.upsert_catalog_items(
        widget.id,
        [_item(0, name="Green tea"), _item(1, name="Stoneware mug"), _item(2, name="Kettle")],
    )
    base = _CAT.format(sid=site.id)

    body = (await admin.get(base, params={"offset": 1, "limit": 1})).json()
    assert body["total"] == 3
    [only] = body["items"]
    assert (only["id"], only["position"], only["source"]) == ("p1", 1, "manual")

    body = (await admin.get(base, params={"q": "mug"})).json()
    assert ([i["id"] for i in body["items"]], body["total"]) == (["p1"], 1)
    assert (await admin.get(base, params={"limit": 201})).status_code == 422


async def test_put_creates_then_replaces_in_place(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store)
    base = _CAT.format(sid=site.id)

    res = await admin.put(f"{base}/items/new-1", json={"name": "New", "price_cents": 900})
    assert res.status_code == 200, res.text
    assert (res.json()["id"], res.json()["position"]) == ("new-1", 3)

    res = await admin.put(f"{base}/items/p0", json={"id": "p0", "name": "Renamed"})
    assert (res.json()["name"], res.json()["position"]) == ("Renamed", 0)

    assert (await admin.put(f"{base}/items/p0", json={"id": "p9", "name": "X"})).status_code == 422
    neg = await admin.put(f"{base}/items/p0", json={"name": "X", "price_cents": -1})
    assert neg.status_code == 422


async def test_bulk_delete_and_reorder_shapes(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store)
    base = _CAT.format(sid=site.id)

    res = await admin.post(
        f"{base}/items:bulk",
        json={"items": [_item(1, name="Again"), {**_item(7), "source": "csv", "position": 99}]},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"upserted": 2, "total": 4}
    [row] = await store.get_catalog_items(widget.id, ["p7"])
    assert (row.position, row.source) == (3, "csv")

    res = await admin.request("DELETE", f"{base}/items", json={"ids": ["p0", "ghost"]})
    assert res.json() == {"deleted": 1, "total": 3}

    res = await admin.post(f"{base}/reorder", json={"ids": ["p7", "p2"]})
    assert res.json() == {"total": 3}
    items, _ = await store.list_catalog(widget.id)
    assert [i.id for i in items] == ["p7", "p2", "p1"]

    dupe = await admin.post(f"{base}/items:bulk", json={"items": [_item(1), _item(1)]})
    assert (dupe.status_code, dupe.json()["detail"]) == (422, "duplicate_id")
    too_many = await admin.post(f"{base}/items:bulk", json={"items": [_item(1)] * 501})
    assert too_many.status_code == 422


async def test_a_full_catalog_is_409_with_its_limit_and_nothing_written(rig, monkeypatch):
    from pocketpaw.paw_bar import catalog_store

    admin, _member, store = rig
    site, widget = await _seeded(store)
    monkeypatch.setattr(catalog_store, "catalog_max_items", lambda: 4)
    res = await admin.post(
        f"{_CAT.format(sid=site.id)}/items:bulk", json={"items": [_item(5), _item(6)]}
    )
    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "catalog_full", "limit": 4}
    assert await store.catalog_count(widget.id) == 3


# --------------------------------------------------------------------------- #
# CSV preview
# --------------------------------------------------------------------------- #


async def test_the_csv_preview_route(rig):
    admin, member, store = rig
    site, _ = await _seeded(store, 0)
    url = f"{_CAT.format(sid=site.id)}/import/csv"
    csv_body = b"name,price,currency\nMug,12.50,USD\n,3,USD\n"

    res = await admin.post(url, files={"file": ("products.csv", csv_body, "text/csv")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["status"], body["source"]) == ("partial", "csv")
    assert [(i["name"], i["price_cents"]) for i in body["items"]] == [("Mug", 1250)]
    assert "line:3:no_name" in body["warnings"]

    big = b"name,price\n" + b"x,1\n" * (2 * 1024 * 1024 // 4 + 10)
    res = await admin.post(url, files={"file": ("big.csv", big, "text/csv")})
    assert (res.status_code, res.json()["detail"]) == (413, "too_large")
    denied = await member.post(url, files={"file": ("p.csv", csv_body, "text/csv")})
    assert denied.status_code == 403


# --------------------------------------------------------------------------- #
# Spec routes: size cap and catalog back-compat
# --------------------------------------------------------------------------- #


def _huge_spec(widget_id: str) -> dict:
    text = "x" * 2000
    blocks = [{"type": "text", "content": text} for _ in range(40)]
    return {"widget_id": widget_id, "pocket_id": "pocket-1", "blocks": blocks}


async def test_both_spec_patches_refuse_a_spec_past_the_cap(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store)
    huge = _huge_spec(widget.id)
    assert len(str(huge)) > MAX_SPEC_BYTES

    res = await admin.patch(f"/paw-bar/admin/site/{site.id}/widget/spec", json={"spec": huge})
    assert (res.status_code, res.json()["detail"]) == (422, "spec_too_large")
    res = await admin.patch(
        f"/paw-bar/widgets/{widget.id}/spec",
        json=huge,
        headers={"X-Paw-Bar-Token": widget.access_token},
    )
    assert (res.status_code, res.json()["detail"]) == (422, "spec_too_large")
    assert (await store.get_widget(widget.id)).spec.blocks == []


async def test_the_cap_does_not_count_the_catalog(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store)
    catalog = [_item(i, description="d" * 300, url=f"/products/{i}") for i in range(200)]
    spec = {"widget_id": widget.id, "pocket_id": "pocket-1", "catalog": catalog}
    assert len(str(spec)) > MAX_SPEC_BYTES

    res = await admin.patch(f"/paw-bar/admin/site/{site.id}/widget/spec", json={"spec": spec})
    assert res.status_code == 200, res.text
    assert res.json()["spec"]["catalog"] == []
    assert await store.catalog_count(widget.id) == 200


async def test_a_spec_patch_adds_its_catalog_items_and_never_deletes(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store)
    url = f"/paw-bar/admin/site/{site.id}/widget/spec"
    blocks = [{"type": "text", "content": "Hello"}]

    absent = {"widget_id": widget.id, "pocket_id": "pocket-1", "blocks": blocks}
    assert (await admin.patch(url, json={"spec": absent})).status_code == 200
    assert (await admin.patch(url, json={"spec": {**absent, "catalog": []}})).status_code == 200
    assert await store.catalog_count(widget.id) == 3

    # An older editor loaded an empty catalog, added one product and saved.
    res = await admin.patch(url, json={"spec": {**absent, "catalog": [_item(8)]}})
    assert res.status_code == 200
    items, _ = await store.list_catalog(widget.id)
    assert [i.id for i in items] == ["p0", "p1", "p2", "p8"]
    assert (await store.get_widget(widget.id)).spec.blocks[0].content == "Hello"

    # An item already there is updated in place: still 4.
    res = await admin.patch(url, json={"spec": {**absent, "catalog": [_item(1, name="New")]}})
    assert res.status_code == 200
    items, _ = await store.list_catalog(widget.id)
    assert [(i.id, i.name) for i in items][:2] == [("p0", "Product 0"), ("p1", "New")]
    assert len(items) == 4


async def test_the_old_client_money_refusal_runs_before_the_catalog_write(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store)
    url = f"/paw-bar/admin/site/{site.id}/widget/spec"
    yen = {**_item(9), "currency": "JPY"}
    body = {"spec": {"widget_id": widget.id, "pocket_id": "pocket-1", "catalog": [yen]}}

    res = await admin.patch(url, json=body)
    assert (res.status_code, res.json()["detail"]) == (409, "currency_units_client_outdated")
    assert await store.catalog_count(widget.id) == 3  # nothing written

    res = await admin.patch(url, json=body, headers={"X-Paw-Money-Units": "iso4217"})
    assert res.status_code == 200
    assert await store.catalog_count(widget.id) == 4


async def test_rollback_refuses_a_revision_past_the_cap(rig, tmp_path):
    import json
    import sqlite3

    admin, _member, store = rig
    _site_doc, widget = await _seeded(store)
    with sqlite3.connect(tmp_path / "catalog_routes.db") as db:
        db.execute(
            "INSERT INTO paw_bar_spec_revisions (widget_id, revision, spec) VALUES (?, 1, ?)",
            (widget.id, json.dumps(_huge_spec(widget.id))),
        )
    res = await admin.post(
        f"/paw-bar/widgets/{widget.id}/spec/rollback",
        headers={"X-Paw-Bar-Token": widget.access_token},
    )
    assert (res.status_code, res.json()["detail"]) == (422, "spec_too_large")


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


async def test_the_overview_carries_a_count_and_no_catalog(rig):
    admin, _member, store = rig
    site, widget = await _seeded(store, 4)
    with (
        patch("pocketpaw_ee.paw_bar.router._count_handoffs", new=AsyncMock(return_value=0)),
        patch("pocketpaw_ee.paw_bar.router._count_conversations", new=AsyncMock(return_value=0)),
    ):
        res = await admin.get(f"/paw-bar/admin/site/{site.id}/overview")
    assert res.status_code == 200, res.text
    view = res.json()["widget"]
    assert (view["catalog_count"], view["spec"]["catalog"]) == (4, [])


async def test_the_frozen_public_spec_is_filled_from_the_store(rig):
    admin, _member, store = rig
    widget = await store.create_widget(
        _widget(
            spec=PawBarSpec(
                widget_id="w", pocket_id="pocket-1", blocks=[PawBarBlock(type="divider")]
            )
        )
    )
    await store.upsert_catalog_items(widget.id, [_item(i) for i in range(205)])
    res = await admin.get(f"/paw-bar/spec/{widget.id}", headers={"Origin": "https://brewco.com"})
    assert res.status_code == 200, res.text
    catalog = res.json()["catalog"]
    assert len(catalog) == 200
    assert catalog[0] == {
        "id": "p0",
        "name": "Product 0",
        "price_cents": 100,
        "currency": "USD",
        "image_url": "",
        "url": "",
        "description": "",
        "in_stock": None,
    }


@pytest.mark.parametrize("path", ["items:bulk", "reorder"])
async def test_write_routes_need_a_body(rig, path):
    admin, _member, store = rig
    site, _ = await _seeded(store)
    assert (await admin.post(f"{_CAT.format(sid=site.id)}/{path}", json={})).status_code == 422


async def test_widget_create_checks_the_spec_size_and_the_catalog_cap(rig, monkeypatch):
    from pocketpaw.paw_bar import store as store_module

    admin, _member, store = rig
    body = {"pocket_id": "pocket-9", "owner": "user:maya", "spec": _huge_spec("w")}
    res = await admin.post("/paw-bar/widgets", json=body)
    assert (res.status_code, res.json()["detail"]) == (422, "spec_too_large")

    monkeypatch.setattr(store_module, "catalog_max_items", lambda: 1)
    spec = {"widget_id": "w", "pocket_id": "pocket-9", "catalog": [_item(1), _item(2)]}
    res = await admin.post("/paw-bar/widgets", json={**body, "spec": spec})
    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "catalog_full", "limit": 1}
    assert await store.list_widgets(pocket_id="pocket-9") == []
