# test_router_pocket_acl.py — POST /uploads pocket ABAC tests.
# Created: 2026-05-03 — Stage 3.E "Files as Knowledge". Verifies the
# upload route honours the pocket-scoped write ACL: members write,
# non-members get a 403, and the no-pocket flow is unchanged.
"""ABAC gate on ``POST /api/v1/uploads`` when ``pocket_id`` is set."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"body"


@pytest.fixture()
def ee_client(tmp_path: Path, beanie_upload_db, monkeypatch):
    """App with EE uploads router, fake auth, and patchable pocket ACL."""
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


def _post_png(client, *, user, ws, pocket_id=None):
    data: dict = {}
    if pocket_id is not None:
        data["pocket_id"] = pocket_id
    return client.post(
        "/api/v1/uploads",
        files=[("files", ("cat.png", PNG, "image/png"))],
        data=data,
        headers={"x-user": user, "x-workspace": ws},
    )


def test_pocket_owner_can_upload(monkeypatch, ee_client):
    """Owner / shared / workspace-visible all grant edit access."""

    async def fake_has_edit_access(*, pocket_id, user_id):
        # Stand-in for the real check: u1 owns pocket PA.
        return pocket_id == "PA" and user_id == "u1"

    from ee.cloud.pockets import service as ps

    monkeypatch.setattr(ps, "has_edit_access", fake_has_edit_access)

    r = _post_png(ee_client, user="u1", ws="w1", pocket_id="PA")
    assert r.status_code == 200, r.text
    assert len(r.json()["uploaded"]) == 1


def test_non_member_gets_403(monkeypatch, ee_client):
    """Non-members get ``files.pocket_forbidden`` — the same error code
    paw-enterprise's pocket UI already handles for read-side denials."""

    async def fake_has_edit_access(**_kwargs):
        return False

    from ee.cloud.pockets import service as ps

    monkeypatch.setattr(ps, "has_edit_access", fake_has_edit_access)

    r = _post_png(ee_client, user="bob", ws="w1", pocket_id="PA")
    assert r.status_code == 403
    assert r.json()["detail"] == "files.pocket_forbidden"


def test_acl_lookup_failure_treated_as_denial(monkeypatch, ee_client):
    """If the ACL lookup raises (Mongo blip, bad pocket id), default to deny."""

    async def fake_has_edit_access(**_kwargs):
        raise RuntimeError("Mongo unreachable")

    from ee.cloud.pockets import service as ps

    monkeypatch.setattr(ps, "has_edit_access", fake_has_edit_access)

    r = _post_png(ee_client, user="u1", ws="w1", pocket_id="PA")
    assert r.status_code == 403


def test_no_pocket_id_unchanged_behaviour(ee_client):
    """No ``pocket_id`` → no pocket ACL check; Stage 1.B path is unchanged."""
    r = _post_png(ee_client, user="u1", ws="w1", pocket_id=None)
    assert r.status_code == 200, r.text
