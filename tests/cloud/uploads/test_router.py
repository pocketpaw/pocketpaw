from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def ee_client(tmp_path: Path, beanie_upload_db, monkeypatch):
    """Build an app with the EE uploads router pointed at a tmp dir and fake deps."""
    import pocketpaw_ee.cloud.uploads.router as uploads_module
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

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
    # Override the license + identity dependencies so tests don't need auth plumbing.
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app.dependency_overrides[require_license] = lambda: None

    # Dynamic user/workspace per-request via headers so we can test isolation.
    from fastapi import Header

    async def _user_dep(x_user: str = Header(default="u1")) -> str:
        return x_user

    async def _workspace_dep(x_workspace: str = Header(default="w1")) -> str:
        return x_workspace

    app.dependency_overrides[current_user_id] = _user_dep
    app.dependency_overrides[current_workspace_id] = _workspace_dep

    app.include_router(uploads_module.router, prefix="/api/v1")
    return TestClient(app)


def _post_png(client: TestClient, user: str, ws: str, filename: str = "cat.png"):
    return client.post(
        "/api/v1/uploads",
        files=[("files", (filename, PNG, "image/png"))],
        headers={"x-user": user, "x-workspace": ws},
    )


def test_upload_roundtrip(ee_client: TestClient):
    r = _post_png(ee_client, "u1", "w1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["uploaded"]) == 1
    fid = data["uploaded"][0]["id"]

    r2 = ee_client.get(
        f"/api/v1/uploads/{fid}",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r2.status_code == 200
    assert r2.content == PNG
    assert "inline" in r2.headers["content-disposition"]


def test_cross_workspace_get_is_404(ee_client: TestClient):
    r = _post_png(ee_client, "u1", "w1")
    fid = r.json()["uploaded"][0]["id"]

    r2 = ee_client.get(
        f"/api/v1/uploads/{fid}",
        headers={"x-user": "u1", "x-workspace": "w2"},
    )
    assert r2.status_code == 404


def test_cross_user_same_workspace_is_404(ee_client: TestClient):
    r = _post_png(ee_client, "alice", "w1")
    fid = r.json()["uploaded"][0]["id"]

    r2 = ee_client.get(
        f"/api/v1/uploads/{fid}",
        headers={"x-user": "bob", "x-workspace": "w1"},
    )
    assert r2.status_code == 404  # owner-only in v1


def test_delete_then_get_is_404(ee_client: TestClient):
    r = _post_png(ee_client, "u1", "w1")
    fid = r.json()["uploaded"][0]["id"]

    r2 = ee_client.delete(
        f"/api/v1/uploads/{fid}",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r2.status_code == 204

    r3 = ee_client.get(
        f"/api/v1/uploads/{fid}",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r3.status_code == 404


def test_bulk_partial_success(ee_client: TestClient):
    r = ee_client.post(
        "/api/v1/uploads",
        files=[
            ("files", ("good.png", PNG, "image/png")),
            ("files", ("bad.svg", b"<svg/>", "image/svg+xml")),
        ],
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["uploaded"]) == 1
    assert len(data["failed"]) == 1
    assert data["failed"][0]["code"] == "unsupported_mime"
