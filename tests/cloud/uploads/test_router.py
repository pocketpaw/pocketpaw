# tests/cloud/uploads/test_router.py
# 2026-07-03 (FL-1 "Library metadata"): added tests that PATCH tags/
# collections/hide_from_ai onto an upload and assert they (a) come back on the
# PATCH response and (b) surface through the uploads provider's list_entries —
# the exact code path the unified GET /files listing renders. Also covers the
# validation errors and the default-on-missing listing shape. Pre-existing
# upload/download/isolation cases are unchanged.
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


async def _seed_upload(fid: str = "seed1", user: str = "u1", ws: str = "w1") -> str:
    """Insert a live upload row directly via the store.

    The guarded ``POST /uploads`` route depends on
    ``require_action_any_workspace`` which the ee_client fixture does not
    override (a pre-existing harness gap unrelated to FL-1 — it 401s in this
    test env). Seeding through the store keeps these FL-1 tests hermetic while
    still exercising the real PATCH route and the real /files read path.
    """
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    from pocketpaw.uploads.file_store import FileRecord

    rec = FileRecord(
        id=fid,
        storage_key=f"chat/202607/{fid}.png",
        filename=f"{fid}.png",
        mime="image/png",
        size=4,
        owner_id=user,
        chat_id=None,
        created=datetime.now(UTC),
    )
    await MongoFileStore().save_scoped(rec, workspace=ws)
    return fid


@pytest.mark.asyncio
async def test_patch_sets_library_metadata(ee_client: TestClient):
    fid = await _seed_upload()

    r2 = ee_client.patch(
        f"/api/v1/uploads/{fid}",
        json={"tags": ["invoice"], "collections": ["Q3"], "hide_from_ai": True},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["tags"] == ["invoice"]
    assert data["collections"] == ["Q3"]
    assert data["hide_from_ai"] is True


@pytest.mark.asyncio
async def test_patch_tag_surfaces_in_files_listing(ee_client: TestClient):
    """Acceptance: PATCH a tag, then the /files provider row carries it.

    Exercises the exact read path the unified ``GET /files`` renders — the
    uploads provider reads ``iter_by_workspace`` and maps it through
    ``_to_entry`` onto the ``FileEntry`` the listing returns.
    """
    from pocketpaw_ee.cloud.files.dto import RequestContext
    from pocketpaw_ee.cloud.files.providers.uploads import UploadsProvider
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    fid = await _seed_upload()

    r2 = ee_client.patch(
        f"/api/v1/uploads/{fid}",
        json={"tags": ["invoice"]},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r2.status_code == 200, r2.text

    provider = UploadsProvider(store=MongoFileStore())
    ctx = RequestContext(user_id="u1", workspace_id="w1", attributes={})
    page = await provider.list_entries(ctx, "/My Files", None, 50, {})
    entries = [e for e in page.items if e.id == f"uploads:{fid}"]
    assert len(entries) == 1
    assert entries[0].tags == ["invoice"]


@pytest.mark.asyncio
async def test_files_listing_defaults_on_untouched_upload(ee_client: TestClient):
    """A never-patched upload lists with empty library metadata, no crash."""
    from pocketpaw_ee.cloud.files.dto import RequestContext
    from pocketpaw_ee.cloud.files.providers.uploads import UploadsProvider
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    fid = await _seed_upload()

    provider = UploadsProvider(store=MongoFileStore())
    ctx = RequestContext(user_id="u1", workspace_id="w1", attributes={})
    page = await provider.list_entries(ctx, "/My Files", None, 50, {})
    entries = [e for e in page.items if e.id == f"uploads:{fid}"]
    assert len(entries) == 1
    assert entries[0].tags == []
    assert entries[0].collections == []
    assert entries[0].hide_from_ai is False


@pytest.mark.asyncio
async def test_patch_rejects_bad_tags_type(ee_client: TestClient):
    fid = await _seed_upload()

    r2 = ee_client.patch(
        f"/api/v1/uploads/{fid}",
        json={"tags": "notalist"},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_patch_rejects_bad_hide_type(ee_client: TestClient):
    fid = await _seed_upload()

    r2 = ee_client.patch(
        f"/api/v1/uploads/{fid}",
        json={"hide_from_ai": "yes"},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r2.status_code == 400


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
