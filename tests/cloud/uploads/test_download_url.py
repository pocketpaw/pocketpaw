# tests/cloud/uploads/test_download_url.py — Coverage for the explicit
# download-url alias added in Cluster E sub-PR 3. Uses the same FastAPI
# app wiring pattern as test_router.py (header-driven user/workspace
# dependencies, tmpfs storage, mongomock-motor metadata).
# Created: 2026-04-19

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def ee_client(tmp_path: Path, beanie_upload_db, monkeypatch):
    """App with the EE uploads router + overridden identity deps."""
    import ee.cloud.uploads.router as uploads_module
    from ee.cloud.uploads.mongo_store import MongoFileStore
    from ee.cloud.uploads.service import EEUploadService
    from pocketpaw.uploads.config import UploadSettings
    from pocketpaw.uploads.local import LocalStorageAdapter

    root = tmp_path / "u"
    root.mkdir()

    test_cfg = UploadSettings(local_root=root)
    test_adapter = LocalStorageAdapter(root=root)
    test_meta = MongoFileStore()
    test_svc = EEUploadService(adapter=test_adapter, meta=test_meta, cfg=test_cfg)

    monkeypatch.setattr(uploads_module, "_SVC", test_svc)

    app = FastAPI()
    from fastapi import Header

    from ee.cloud.license import require_license
    from ee.cloud.shared.deps import current_user_id, current_workspace_id

    app.dependency_overrides[require_license] = lambda: None

    async def _user_dep(x_user: str = Header(default="u1")) -> str:
        return x_user

    async def _workspace_dep(x_workspace: str = Header(default="w1")) -> str:
        return x_workspace

    app.dependency_overrides[current_user_id] = _user_dep
    app.dependency_overrides[current_workspace_id] = _workspace_dep

    app.include_router(uploads_module.router, prefix="/api/v1")
    return TestClient(app)


def _upload(client: TestClient, user: str, ws: str, name: str = "pic.png") -> str:
    r = client.post(
        "/api/v1/uploads",
        files=[("files", (name, PNG, "image/png"))],
        headers={"x-user": user, "x-workspace": ws},
    )
    assert r.status_code == 200, r.text
    return r.json()["uploaded"][0]["id"]


def test_download_url_returns_ttl_and_filename(ee_client: TestClient):
    """The alias returns a signed-or-cookie URL, an expires_at in the
    near future, and the original filename for the Save-As dialog."""
    fid = _upload(ee_client, "u1", "w1", name="report.png")

    r = ee_client.get(
        f"/api/v1/uploads/{fid}/download-url",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["url"].endswith(f"/uploads/{fid}") or body["url"].startswith("http")
    assert body["filename"] == "report.png"

    now = int(time.time())
    # TTL should be short (default is 15 minutes) but always in the
    # future and not more than an hour out.
    assert now < body["expires_at"] <= now + 60 * 60


def test_download_url_blocks_cross_workspace(ee_client: TestClient):
    """A file uploaded in workspace A must not be fetchable from B,
    even through the download-url alias."""
    fid = _upload(ee_client, "u1", "w-a")

    r = ee_client.get(
        f"/api/v1/uploads/{fid}/download-url",
        headers={"x-user": "u1", "x-workspace": "w-b"},
    )
    assert r.status_code == 404


def test_download_url_rejects_path_traversal_ids(ee_client: TestClient):
    """File ids are opaque strings in our store. Callers can't smuggle a
    traversal string through the path — FastAPI routes it as the id,
    the store's workspace-scoped lookup returns None -> 404."""
    r = ee_client.get(
        "/api/v1/uploads/..%2F..%2Fetc%2Fpasswd/download-url",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 404


def test_download_url_404_for_deleted(ee_client: TestClient):
    """Soft-deleted files disappear from the alias just like they do
    from /grant and /{file_id}."""
    fid = _upload(ee_client, "u1", "w1")

    d = ee_client.delete(
        f"/api/v1/uploads/{fid}",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert d.status_code == 204

    r = ee_client.get(
        f"/api/v1/uploads/{fid}/download-url",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 404
