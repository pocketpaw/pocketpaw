"""Folder CRUD + upload path integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def folder_client(tmp_path: Path, beanie_upload_db, monkeypatch):
    import ee.cloud.uploads.router as uploads_module
    from ee.cloud.uploads.folder_store import FolderStore
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
    monkeypatch.setattr(uploads_module, "_META", test_meta)
    monkeypatch.setattr(uploads_module, "_FOLDERS", FolderStore())

    # Admin check — default to False unless the test overrides.
    admins: set[tuple[str, str]] = set()

    async def fake_is_admin(user_id: str, workspace: str) -> bool:
        return (user_id, workspace) in admins

    monkeypatch.setattr(uploads_module, "_is_workspace_admin", fake_is_admin)

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
    client = TestClient(app)
    client.admins = admins  # type: ignore[attr-defined]
    return client


def _hdr(user="u1", ws="w1"):
    return {"x-user": user, "x-workspace": ws}


def test_create_folder_root_child(folder_client: TestClient):
    r = folder_client.post("/api/v1/uploads/folders", json={"path": "/reports"}, headers=_hdr())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["path"] == "/reports"
    assert data["name"] == "reports"


def test_create_folder_requires_parent(folder_client: TestClient):
    # Missing parent ("/reports") — rejected.
    r = folder_client.post(
        "/api/v1/uploads/folders",
        json={"path": "/reports/2026"},
        headers=_hdr(),
    )
    assert r.status_code == 400


def test_create_folder_duplicate_409(folder_client: TestClient):
    folder_client.post("/api/v1/uploads/folders", json={"path": "/a"}, headers=_hdr())
    r = folder_client.post("/api/v1/uploads/folders", json={"path": "/a"}, headers=_hdr())
    assert r.status_code == 409


def test_upload_auto_creates_folder_chain(folder_client: TestClient):
    r = folder_client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        data={"path": "/reports/2026/q2"},
        headers=_hdr(),
    )
    assert r.status_code == 200, r.text
    # All three folders should now exist.
    for p in ("/reports", "/reports/2026", "/reports/2026/q2"):
        # creating should 409 because it exists
        rr = folder_client.post(
            "/api/v1/uploads/folders", json={"path": p}, headers=_hdr()
        )
        assert rr.status_code == 409, f"{p}: {rr.status_code} {rr.text}"


def test_rename_folder_rewrites_descendants(folder_client: TestClient):
    # Seed
    folder_client.post("/api/v1/uploads/folders", json={"path": "/reports"}, headers=_hdr())
    folder_client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        data={"path": "/reports/2026"},
        headers=_hdr(),
    )
    # Find the /reports folder id.
    # Use list_children via provider path listing — hit internal store directly.
    from ee.cloud.uploads.folder_store import FolderStore

    store = FolderStore()

    import asyncio

    async def _get_id():
        doc = await store.get_by_path("w1", "/reports")
        return doc.folder_id

    fid = asyncio.get_event_loop().run_until_complete(_get_id())

    r = folder_client.patch(
        f"/api/v1/uploads/folders/{fid}",
        json={"new_path": "/archive"},
        headers=_hdr(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["path"] == "/archive"

    # The auto-created /reports/2026 child should now be /archive/2026.
    async def _check():
        return await store.get_by_path("w1", "/archive/2026")

    moved = asyncio.get_event_loop().run_until_complete(_check())
    assert moved is not None


def test_delete_folder_not_empty_409(folder_client: TestClient):
    folder_client.post("/api/v1/uploads/folders", json={"path": "/a"}, headers=_hdr())
    folder_client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        data={"path": "/a"},
        headers=_hdr(),
    )
    import asyncio

    from ee.cloud.uploads.folder_store import FolderStore

    async def _fid():
        return (await FolderStore().get_by_path("w1", "/a")).folder_id

    fid = asyncio.get_event_loop().run_until_complete(_fid())
    r = folder_client.delete(
        f"/api/v1/uploads/folders/{fid}?cascade=false", headers=_hdr()
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "folder.not_empty"


def test_delete_folder_cascade_softdeletes_files(folder_client: TestClient):
    folder_client.post("/api/v1/uploads/folders", json={"path": "/a"}, headers=_hdr())
    r_up = folder_client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        data={"path": "/a"},
        headers=_hdr(),
    )
    fid_file = r_up.json()["uploaded"][0]["id"]
    import asyncio

    from ee.cloud.uploads.folder_store import FolderStore

    async def _fid():
        return (await FolderStore().get_by_path("w1", "/a")).folder_id

    fid = asyncio.get_event_loop().run_until_complete(_fid())
    r = folder_client.delete(
        f"/api/v1/uploads/folders/{fid}?cascade=true", headers=_hdr()
    )
    assert r.status_code == 204
    # File is soft-deleted → GET 404.
    r2 = folder_client.get(f"/api/v1/uploads/{fid_file}", headers=_hdr())
    assert r2.status_code == 404


def test_folder_rename_stranger_forbidden(folder_client: TestClient):
    folder_client.post("/api/v1/uploads/folders", json={"path": "/a"}, headers=_hdr(user="u1"))
    import asyncio

    from ee.cloud.uploads.folder_store import FolderStore

    async def _fid():
        return (await FolderStore().get_by_path("w1", "/a")).folder_id

    fid = asyncio.get_event_loop().run_until_complete(_fid())
    r = folder_client.patch(
        f"/api/v1/uploads/folders/{fid}",
        json={"new_path": "/b"},
        headers=_hdr(user="u2"),
    )
    assert r.status_code == 403


def test_folder_rename_admin_allowed(folder_client: TestClient):
    folder_client.post("/api/v1/uploads/folders", json={"path": "/a"}, headers=_hdr(user="u1"))
    folder_client.admins.add(("admin", "w1"))  # type: ignore[attr-defined]
    import asyncio

    from ee.cloud.uploads.folder_store import FolderStore

    async def _fid():
        return (await FolderStore().get_by_path("w1", "/a")).folder_id

    fid = asyncio.get_event_loop().run_until_complete(_fid())
    r = folder_client.patch(
        f"/api/v1/uploads/folders/{fid}",
        json={"new_path": "/b"},
        headers=_hdr(user="admin"),
    )
    assert r.status_code == 200


def test_patch_file_folder_path(folder_client: TestClient):
    folder_client.post("/api/v1/uploads/folders", json={"path": "/x"}, headers=_hdr())
    r_up = folder_client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        headers=_hdr(),
    )
    fid = r_up.json()["uploaded"][0]["id"]
    r = folder_client.patch(
        f"/api/v1/uploads/{fid}",
        json={"folder_path": "/x", "filename": "renamed.png"},
        headers=_hdr(),
    )
    assert r.status_code == 200
    assert r.json()["filename"] == "renamed.png"


def test_patch_file_stranger_forbidden(folder_client: TestClient):
    r_up = folder_client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        headers=_hdr(user="u1"),
    )
    fid = r_up.json()["uploaded"][0]["id"]
    r = folder_client.patch(
        f"/api/v1/uploads/{fid}",
        json={"filename": "x.png"},
        headers=_hdr(user="u2"),
    )
    assert r.status_code == 403
