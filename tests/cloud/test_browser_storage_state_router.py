# test_browser_storage_state_router.py — the /browser storage-state import route.
# Created: 2026-09-06 (BR-5, feat/browser-surface-profile).
#
# The route handles a credential-equivalent secret, so these are the security
# assertions: admin-only, scoped to the PATH workspace (one tenant cannot reach
# another's), GET never carries a cookie value anywhere in its serialized body,
# and a hostile import is refused with a message that names the field but not
# the value — and writes nothing.
"""Tests for the workspace browser storage-state router."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.browser import profile
from tests.cloud.conftest import override_workspace_role

SECRET = "secret-value-XYZ123"
STATE = {
    "cookies": [{"name": "session", "value": SECRET, "domain": "portal.example.test", "path": "/"}],
    "origins": [
        {
            "origin": "https://portal.example.test",
            "localStorage": [{"name": "tok", "value": SECRET}],
        }
    ],
}
URL = "/workspaces/w1/browser/storage-state"


@pytest.fixture(autouse=True)
def profiles_home(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketpaw.config.get_config_dir", lambda: tmp_path)
    return tmp_path


def _app(role: str = "admin", workspace_id: str = "w1") -> FastAPI:
    from pocketpaw_ee.cloud.browser.router import router

    app = FastAPI()
    app.include_router(router)
    override_workspace_role(app, role=role, workspace_id=workspace_id)
    return app


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        yield c


async def test_import_then_read_back_counts_only(client):
    put = await client.put(URL, json=STATE)
    assert put.status_code == 200

    got = await client.get(URL)
    assert got.status_code == 200
    body = got.json()
    assert body["cookie_count"] == 1
    assert body["domains"] == ["portal.example.test"]
    assert body["origin_count"] == 1
    assert body["imported_at"]

    # The whole serialized body, not just the fields we thought to check.
    assert SECRET not in json.dumps(body)
    assert SECRET not in got.text
    assert SECRET not in put.text


async def test_the_imported_cookie_is_on_disk_for_the_browser(client):
    await client.put(URL, json=STATE)
    assert profile.read_state("w1")["cookies"][0]["value"] == SECRET


async def test_get_is_404_before_anything_is_imported(client):
    assert (await client.get(URL)).status_code == 404


async def test_delete_forgets_the_profile(client):
    await client.put(URL, json=STATE)
    assert (await client.delete(URL)).status_code == 204
    assert profile.read_state("w1") is None
    assert (await client.get(URL)).status_code == 404


async def test_a_bare_cookie_array_is_accepted(client):
    put = await client.put(URL, json=STATE["cookies"])
    assert put.status_code == 200
    assert put.json()["cookie_count"] == 1


@pytest.mark.parametrize(
    "hostile",
    [
        [{"name": "a", "value": SECRET, "domain": ".com", "path": "/"}],
        [{"name": "a", "value": SECRET}],
        {"cookies": "nope"},
        {"cookies": []},
    ],
)
async def test_a_hostile_import_is_refused_and_writes_nothing(client, hostile):
    resp = await client.put(URL, json=hostile)

    assert resp.status_code == 422
    assert SECRET not in resp.text
    assert profile.read_state("w1") is None
    assert not profile.state_path("w1").exists()


async def test_unparseable_json_is_refused(client):
    resp = await client.put(
        URL, content=b"{ not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 422
    assert not profile.state_path("w1").exists()


async def test_an_oversized_body_is_refused(client):
    huge = [
        {"name": f"c{i}", "value": "x" * 2000, "domain": "portal.example.test", "path": "/"}
        for i in range(400)
    ]
    resp = await client.put(URL, json=huge)

    assert resp.status_code == 422
    assert not profile.state_path("w1").exists()


async def test_a_member_cannot_import(profiles_home):
    app = _app(role="member")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.put(URL, json=STATE)).status_code == 403
    assert not profile.state_path("w1").exists()


async def test_one_workspace_cannot_touch_anothers_state(profiles_home):
    """The caller is pinned to w1; every verb against w2 is refused and w2's
    state is untouched."""
    profile.write_state("w2", profile.validate_storage_state(STATE))
    app = _app(workspace_id="w1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        other = "/workspaces/w2/browser/storage-state"
        assert (await c.get(other)).status_code == 403
        assert (await c.put(other, json=STATE)).status_code == 403
        assert (await c.delete(other)).status_code == 403

    assert profile.read_state("w2") is not None
