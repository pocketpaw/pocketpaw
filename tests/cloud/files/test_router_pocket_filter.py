# test_router_pocket_filter.py — GET /files pocket-filter ABAC tests.
# Created: 2026-05-03 — Stage 3.E "Files as Knowledge". Verifies the
# unified files endpoint honours the pocket-read ACL and that the
# unified service filter narrows to the pocket's rows.
"""ABAC + filter behaviour on ``GET /api/v1/files?pocket_id=...``."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud.uploads.models import FileUpload
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

from pocketpaw.uploads.file_store import FileRecord


@pytest.fixture()
async def beanie_files_db():
    db_name = f"test_files_pocket_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[FileUpload])
    yield db


async def _seed(workspace: str, *, name: str, pocket_id: str | None = None) -> str:
    store = MongoFileStore()
    rec = FileRecord(
        id=uuid.uuid4().hex,
        storage_key=f"keys/{uuid.uuid4().hex}",
        filename=name,
        mime="text/plain",
        size=11,
        owner_id="u1",
        chat_id=None,
        created=datetime.now(),
    )
    await store.save_scoped(rec, workspace=workspace, pocket_id=pocket_id)
    return rec.id


def _build_app(monkeypatch, *, member_check):
    """Mount the files router with patched auth + pocket ACL."""
    from pocketpaw_ee.cloud.files.router import router as files_router

    app = FastAPI()
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app.dependency_overrides[require_license] = lambda: None

    async def _user_dep(x_user: str = Header(default="u1")) -> str:
        return x_user

    async def _workspace_dep(x_workspace: str = Header(default="w1")) -> str:
        return x_workspace

    app.dependency_overrides[current_user_id] = _user_dep
    app.dependency_overrides[current_workspace_id] = _workspace_dep

    from pocketpaw_ee.cloud.pockets import service as ps

    monkeypatch.setattr(ps, "is_member", member_check)

    app.include_router(files_router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.asyncio
async def test_member_lists_pocket_files(monkeypatch, beanie_files_db):
    """Member of pocket A → GET /files?pocket_id=A returns A's rows."""
    await _seed("w1", name="ws.pdf")
    await _seed("w1", name="a-secret.pdf", pocket_id="A")
    await _seed("w1", name="b-secret.pdf", pocket_id="B")

    async def is_member(*, pocket_id, user_id):
        return pocket_id == "A" and user_id == "u1"

    client = _build_app(monkeypatch, member_check=is_member)
    r = client.get(
        "/api/v1/files?pocket_id=A&source=chat",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["pocket_id"] == "A"
    names = [f["filename"] for f in payload["files"]]
    assert names == ["a-secret.pdf"]


@pytest.mark.asyncio
async def test_non_member_gets_403(monkeypatch, beanie_files_db):
    await _seed("w1", name="a-secret.pdf", pocket_id="A")

    async def is_member(**_kwargs):
        return False

    client = _build_app(monkeypatch, member_check=is_member)
    r = client.get(
        "/api/v1/files?pocket_id=A",
        headers={"x-user": "bob", "x-workspace": "w1"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "files.pocket_forbidden"


@pytest.mark.asyncio
async def test_no_pocket_id_returns_workspace_only(monkeypatch, beanie_files_db):
    """Without ``pocket_id`` the listing returns workspace-only rows;
    pocket files don't bleed into the workspace Files panel."""
    await _seed("w1", name="ws.pdf")
    await _seed("w1", name="a-secret.pdf", pocket_id="A")
    await _seed("w1", name="b-secret.pdf", pocket_id="B")

    async def is_member(**_kwargs):  # not consulted
        return False

    client = _build_app(monkeypatch, member_check=is_member)
    r = client.get(
        "/api/v1/files?source=chat",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    names = [f["filename"] for f in r.json()["files"]]
    assert names == ["ws.pdf"]


@pytest.mark.asyncio
async def test_acl_lookup_failure_treated_as_denial(monkeypatch, beanie_files_db):
    await _seed("w1", name="a-secret.pdf", pocket_id="A")

    async def is_member(**_kwargs):
        raise RuntimeError("Mongo blip")

    client = _build_app(monkeypatch, member_check=is_member)
    r = client.get(
        "/api/v1/files?pocket_id=A",
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 403
