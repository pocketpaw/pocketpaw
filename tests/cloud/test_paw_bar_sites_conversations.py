# tests/cloud/test_paw_bar_sites_conversations.py — POST
# /paw-bar/admin/sites/conversations, the batch form of the per-site GET.
# Created 2026-09-27 (feat/bulk-grants-conversations): each site in the batch
# answers exactly what GET /paw-bar/admin/site/{id}/conversations answers;
# one site's failure (absent, malformed, another workspace's, or a crash) lands
# in ``errors`` without failing the others; ids are deduped; a bad ``state`` and
# out-of-range bodies 422; the ADMIN-only read gate still applies. Builders and
# app wiring come from test_paw_bar_conversations.py.

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.store import PawBarStore
from tests.cloud.test_paw_bar_conversations import _build_app, _mk_run, _site, _widget

_URL = "/paw-bar/admin/sites/conversations"


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "batch.db")


@pytest_asyncio.fixture
async def client(mongo_db, store, monkeypatch):
    app = _build_app(store, monkeypatch, role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


@pytest.mark.asyncio
async def test_each_site_matches_the_single_get(client):
    c, store = client
    one = await _site()
    two = await _site(pocket_id="pocket-2", name="Second")
    await store.create_widget(_widget())
    await store.create_widget(_widget(pocket_id="pocket-2"))
    await _mk_run(user_id="cust-a")
    await _mk_run(user_id="cust-b", scope_id="pocket-2", partial_text="Two.")

    res = await c.post(_URL, json={"site_ids": [str(one.id), str(two.id)]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["errors"] == {}
    assert set(body["sites"]) == {str(one.id), str(two.id)}
    for site in (one, two):
        single = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()
        assert body["sites"][str(site.id)] == single
    assert body["sites"][str(two.id)]["items"][0]["preview"] == "Two."


@pytest.mark.asyncio
async def test_limit_and_state_are_forwarded(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run(user_id="cust-open")
    await _mk_run(user_id="cust-closed")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-open", "ws-1")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-closed", "ws-1")
    await store.update_conversation(widget.id, "cust-closed", workspace_id="ws-1", state="closed")

    body = (await c.post(_URL, json={"site_ids": [str(site.id)], "state": "closed"})).json()
    page = body["sites"][str(site.id)]
    assert [i["customer_ref"] for i in page["items"]] == ["cust-closed"]
    assert page["counts"]["open"] == 1

    body = (await c.post(_URL, json={"site_ids": [str(site.id)], "limit": 1})).json()
    assert len(body["sites"][str(site.id)]["items"]) == 1


@pytest.mark.asyncio
async def test_failures_are_isolated_per_site(client, monkeypatch):
    c, store = client
    good = await _site()
    foreign = await _site(workspace="ws-other", pocket_id="pocket-x")
    broken = await _site(pocket_id="pocket-boom")
    await store.create_widget(_widget())
    await _mk_run()

    import pocketpaw_ee.paw_bar.router as router_module

    real = router_module._list_conversations

    async def _flaky(pocket_id, *args, **kwargs):
        if pocket_id == "pocket-boom":
            raise RuntimeError("boom")
        return await real(pocket_id, *args, **kwargs)

    monkeypatch.setattr(router_module, "_list_conversations", _flaky)

    ids = [str(good.id), "not-an-object-id", "0" * 24, str(foreign.id), str(broken.id)]
    res = await c.post(_URL, json={"site_ids": ids})
    assert res.status_code == 200, res.text
    body = res.json()
    assert list(body["sites"]) == [str(good.id)]
    assert len(body["sites"][str(good.id)]["items"]) == 1
    assert body["errors"] == {
        "not-an-object-id": "not_found",
        "0" * 24: "not_found",
        # Another workspace's site reads exactly like a missing one.
        str(foreign.id): "not_found",
        str(broken.id): "error",
    }


@pytest.mark.asyncio
async def test_site_ids_are_deduped(client, monkeypatch):
    c, store = client
    site = await _site()
    await store.create_widget(_widget())

    import pocketpaw_ee.paw_bar.router as router_module

    real = router_module._resolve_site_and_widget
    calls: list[str] = []

    async def _counting(site_id, workspace_id):
        calls.append(site_id)
        return await real(site_id, workspace_id)

    monkeypatch.setattr(router_module, "_resolve_site_and_widget", _counting)

    res = await c.post(_URL, json={"site_ids": [str(site.id)] * 3})
    assert res.status_code == 200
    assert list(res.json()["sites"]) == [str(site.id)]
    assert calls == [str(site.id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"site_ids": []},
        {"site_ids": [f"s{i}" for i in range(51)]},
        {"site_ids": ["s"], "limit": 0},
        {"site_ids": ["s"], "limit": 101},
        {"site_ids": ["s"], "state": "banana"},
    ],
)
async def test_bad_bodies_are_422(client, body):
    c, _store = client
    res = await c.post(_URL, json=body)
    assert res.status_code == 422
    if "state" in body:
        assert res.json()["detail"] == "invalid_state"


@pytest.mark.asyncio
async def test_member_role_is_refused(mongo_db, store, monkeypatch):
    app = _build_app(store, monkeypatch, role="member")
    site = await _site()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.post(_URL, json={"site_ids": [str(site.id)]})
    assert res.status_code == 403
