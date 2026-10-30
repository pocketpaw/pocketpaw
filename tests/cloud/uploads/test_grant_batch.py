# tests/cloud/uploads/test_grant_batch.py — POST /uploads/grants, the batch form
# of GET /uploads/{file_id}/grant.
# Created 2026-09-27 (feat/bulk-grants-conversations): covers request-order
# output with a per-item not_found (missing, foreign-workspace), batch == single
# for a thumbnail and a presigned full-size item, dedupe of identical items,
# the 1..200 / w / h / q bounds (422), and that the literal /grants path is not
# captured by a /{file_id} route. App wiring copied from test_download_url.py.

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from tests.cloud.uploads.conftest import install_workspace_caller

PNG = b"\x89PNG\r\n\x1a\n" + b"body"
H = {"x-user": "u1", "x-workspace": "w1"}


@pytest.fixture()
def svc_and_client(tmp_path: Path, beanie_upload_db, monkeypatch):
    import pocketpaw_ee.cloud.uploads.router as uploads_module
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    from pocketpaw.uploads.config import UploadSettings
    from pocketpaw.uploads.local import LocalStorageAdapter

    root = tmp_path / "u"
    root.mkdir()
    svc = EEUploadService(
        adapter=LocalStorageAdapter(root=root),
        meta=MongoFileStore(),
        cfg=UploadSettings(local_root=root),
    )
    monkeypatch.setattr(uploads_module, "_SVC", svc)

    app = FastAPI()
    app.dependency_overrides[require_license] = lambda: None

    async def _user_dep(x_user: str = Header(default="u1")) -> str:
        return x_user

    async def _workspace_dep(x_workspace: str = Header(default="w1")) -> str:
        return x_workspace

    app.dependency_overrides[current_user_id] = _user_dep
    app.dependency_overrides[current_workspace_id] = _workspace_dep
    install_workspace_caller(app)
    app.include_router(uploads_module.router, prefix="/api/v1")
    return svc, TestClient(app)


@pytest.fixture()
def client(svc_and_client) -> TestClient:
    return svc_and_client[1]


def _upload(client: TestClient, ws: str = "w1") -> str:
    r = client.post(
        "/api/v1/uploads",
        files=[("files", ("pic.png", PNG, "image/png"))],
        headers={"x-user": "u1", "x-workspace": ws},
    )
    assert r.status_code == 200, r.text
    return r.json()["uploaded"][0]["id"]


def _batch(client: TestClient, items: list[dict]):
    return client.post("/api/v1/uploads/grants", json={"items": items}, headers=H)


def test_order_is_preserved_and_not_found_is_per_item(client: TestClient):
    a = _upload(client)
    b = _upload(client)
    foreign = _upload(client, ws="w-other")

    r = _batch(
        client,
        [
            {"id": b, "w": 64, "h": 64},
            {"id": "does-not-exist"},
            {"id": a},
            {"id": foreign},
        ],
    )
    assert r.status_code == 200, r.text
    grants = r.json()["grants"]

    assert [g["id"] for g in grants] == [b, "does-not-exist", a, foreign]
    assert grants[0]["url"] == f"/api/v1/uploads/{b}?w=64&h=64&q=80&f=webp"
    assert (grants[0]["w"], grants[0]["h"], grants[0]["q"], grants[0]["f"]) == (64, 64, 80, "webp")
    assert grants[1] == {
        "id": "does-not-exist",
        "w": 0,
        "h": 0,
        "q": 80,
        "f": "webp",
        "error": "not_found",
    }
    assert grants[2]["url"] == f"/api/v1/uploads/{a}"
    # Another workspace's file reads exactly like a missing one — never leaks.
    assert grants[3]["error"] == "not_found"
    assert "url" not in grants[3]


def test_batch_thumbnail_matches_single_grant(client: TestClient):
    fid = _upload(client)
    single = client.get(f"/api/v1/uploads/{fid}/grant?w=120&h=90&q=70&f=jpeg", headers=H).json()
    batch = _batch(client, [{"id": fid, "w": 120, "h": 90, "q": 70, "f": "jpeg"}]).json()
    item = batch["grants"][0]

    assert item["url"] == single["url"]
    assert abs(item["expires_at"] - single["expires_at"]) <= 2


def test_batch_full_size_matches_single_grant_when_presigned(svc_and_client, monkeypatch):
    svc, client = svc_and_client
    fid = _upload(client)
    real = svc.presigned_get

    async def _presigned(file_id, user_id, workspace, ttl):
        rec, _ = await real(file_id, user_id, workspace, ttl)
        return rec, f"https://bucket.example/{file_id}?sig=abc"

    monkeypatch.setattr(svc, "presigned_get", _presigned)

    single = client.get(f"/api/v1/uploads/{fid}/grant", headers=H).json()
    item = _batch(client, [{"id": fid}]).json()["grants"][0]
    assert single["url"] == f"https://bucket.example/{fid}?sig=abc"
    assert item["url"] == single["url"]
    assert abs(item["expires_at"] - single["expires_at"]) <= 2

    # A thumbnail of the same file still gets the cookie-authed server URL.
    thumb = _batch(client, [{"id": fid, "w": 32}]).json()["grants"][0]
    assert thumb["url"] == f"/api/v1/uploads/{fid}?w=32&h=0&q=80&f=webp"


def test_identical_items_are_minted_once(svc_and_client, monkeypatch):
    svc, client = svc_and_client
    fid = _upload(client)
    real = svc.presigned_get
    calls: list[str] = []

    async def _counting(file_id, user_id, workspace, ttl):
        calls.append(file_id)
        return await real(file_id, user_id, workspace, ttl)

    monkeypatch.setattr(svc, "presigned_get", _counting)

    item = {"id": fid, "w": 48, "h": 48}
    grants = _batch(client, [item, {"id": fid}, item]).json()["grants"]
    assert len(grants) == 3
    assert grants[0] == grants[2]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"id": "x"}] * 201,
        [{"id": "x", "w": 2049}],
        [{"id": "x", "h": -1}],
        [{"id": "x", "q": 0}],
        [{"id": "x", "q": 101}],
    ],
)
def test_bounds_are_422(client: TestClient, items):
    assert _batch(client, items).status_code == 422


def test_two_hundred_items_are_accepted(client: TestClient):
    r = _batch(client, [{"id": f"missing-{i}"} for i in range(200)])
    assert r.status_code == 200
    assert len(r.json()["grants"]) == 200


def test_grants_path_is_not_a_file_id(client: TestClient):
    """``/grants`` is its own route: a GET of it is the download route looking up
    a file literally named "grants" (404), and the POST never reaches it."""
    assert client.get("/api/v1/uploads/grants", headers=H).status_code == 404
    assert _batch(client, [{"id": "grants"}]).json()["grants"][0]["error"] == "not_found"
