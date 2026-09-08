# test_links.py — GET /files/{id}/links and GET /files/graph (files vault, B3).
# Created: 2026-09-05 (feat/files-links). Router-level tests with the real
#   kb.read guard running (override_workspace_role) against a mongomock store:
#   outgoing links resolve by normalized stem (alias and .MD casing included),
#   backlinks come back, a cross-workspace id is 404 file.not_found, another
#   tenant's note never shows as a backlink, pocket-private notes stay out of
#   workspace backlinks, the graph carries nodes + deduped edges + ghosts and
#   reports truncation at the cap, and /graph applies the pocket membership
#   rule GET /files and POST /files/search use (same helper, same 403 body).
"""Tests for the files vault link reads."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from beanie import init_beanie
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud.files import service as files_service
from pocketpaw_ee.cloud.uploads.links import parse_note_links
from pocketpaw_ee.cloud.uploads.models import FileUpload
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

from pocketpaw.uploads.file_store import FileRecord
from tests.cloud.conftest import override_workspace_role

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def links_db():
    db = AsyncMongoMockClient()[f"test_links_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[FileUpload])
    yield db


async def _note(
    workspace: str,
    name: str,
    text: str = "",
    *,
    pocket_id: str | None = None,
    age_minutes: int = 0,
    file_id: str | None = None,
) -> str:
    store = MongoFileStore()
    rec = FileRecord(
        id=file_id or uuid.uuid4().hex,
        storage_key=f"editor/{uuid.uuid4().hex}",
        filename=name,
        mime="text/markdown",
        size=len(text),
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )
    await store.save_scoped(rec, workspace=workspace, pocket_id=pocket_id)
    doc = await store.get_doc_scoped(rec.id, workspace)
    doc.createdAt = rec.created
    doc.link_names = list(parse_note_links(text).link_names)
    await doc.save()
    return rec.id


def _client(monkeypatch, *, workspace: str = "w1", role: str = "member", member=None):
    from pocketpaw_ee.cloud.files.router import router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    app = FastAPI()
    app.dependency_overrides[require_license] = lambda: None
    override_workspace_role(app, role=role, workspace_id=workspace)
    if member is not None:
        monkeypatch.setattr(pockets_service, "is_member", member)
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


async def test_outgoing_links_resolve_by_normalized_stem(monkeypatch, links_db):
    a = await _note("w1", "A.md", "see [[Beta|alias]] and [[gamma.md]] and [[Ghost]]")
    b = await _note("w1", "Beta.MD")
    g = await _note("w1", "Gamma.md")

    r = _client(monkeypatch).get(f"/api/v1/files/{a}/links")
    assert r.status_code == 200, r.text
    out = r.json()["outgoing"]
    assert [(o["name"], o["file_id"], o["filename"]) for o in out] == [
        ("beta", b, "Beta.MD"),
        ("gamma", g, "Gamma.md"),
        ("ghost", None, None),
    ]


async def test_backlinks_come_back_and_exclude_self(monkeypatch, links_db):
    a = await _note("w1", "A.md", "[[A]] links to itself")
    b = await _note("w1", "B.md", "mentions [[a]]")
    await _note("w1", "C.md", "no links")

    r = _client(monkeypatch).get(f"/api/v1/files/{a}/links")
    assert r.status_code == 200, r.text
    assert [(x["file_id"], x["filename"], x["mime"]) for x in r.json()["backlinks"]] == [
        (b, "B.md", "text/markdown")
    ]


async def test_cross_workspace_id_is_404_and_never_a_backlink(monkeypatch, links_db):
    a = await _note("w1", "A.md")
    await _note("w2", "Spy.md", "[[A]] from another tenant")

    c1 = _client(monkeypatch, workspace="w1")
    assert c1.get(f"/api/v1/files/{a}/links").json()["backlinks"] == []

    r = _client(monkeypatch, workspace="w2").get(f"/api/v1/files/{a}/links")
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "file.not_found"


async def test_pocket_private_note_is_not_a_workspace_backlink(monkeypatch, links_db):
    a = await _note("w1", "A.md")
    await _note("w1", "Secret.md", "[[A]]", pocket_id="p1")

    r = _client(monkeypatch).get(f"/api/v1/files/{a}/links")
    assert r.json()["backlinks"] == []


async def test_same_stem_newest_wins(monkeypatch, links_db):
    a = await _note("w1", "A.md", "[[dup]]")
    await _note("w1", "Dup.md", age_minutes=60)
    newest = await _note("w1", "dup.md", age_minutes=0)

    r = _client(monkeypatch).get(f"/api/v1/files/{a}/links")
    assert r.json()["outgoing"][0]["file_id"] == newest


async def test_graph_nodes_edges_ghosts(monkeypatch, links_db):
    a = await _note("w1", "A.md", "[[B]] [[b]] [[Ghost]] [[ghost]]")
    b = await _note("w1", "B.md", "[[A]]")
    await _note("w1", "Secret.md", "[[A]]", pocket_id="p1")

    r = _client(monkeypatch).get("/api/v1/files/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(n["id"] for n in body["nodes"]) == sorted([a, b])
    assert sorted((e["source"], e["target"]) for e in body["edges"]) == sorted([(a, b), (b, a)])
    assert body["ghosts"] == ["ghost"]
    assert body["truncated"] is False


async def test_graph_truncates_at_cap(monkeypatch, links_db):
    for i in range(3):
        await _note("w1", f"N{i}.md", age_minutes=i)
    monkeypatch.setattr(files_service, "GRAPH_CAP", 2)

    body = _client(monkeypatch).get("/api/v1/files/graph").json()
    assert len(body["nodes"]) == 2
    assert body["truncated"] is True


async def test_graph_pocket_membership(monkeypatch, links_db):
    await _note("w1", "Open.md")
    s = await _note("w1", "Secret.md", pocket_id="p1")

    async def deny(**_kw):
        return False

    r = _client(monkeypatch, member=deny).get("/api/v1/files/graph?pocket_id=p1")
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "files.pocket_forbidden"

    async def allow(**_kw):
        return True

    body = _client(monkeypatch, member=allow).get("/api/v1/files/graph?pocket_id=p1").json()
    assert [n["id"] for n in body["nodes"]] == [s]


async def test_editor_id_with_slash_reaches_the_links_route(monkeypatch, links_db):
    """write_file stores ``ws:path`` ids; a daily note's path carries a slash."""
    daily = await _note("w1", "2026-09-05.md", "[[Plan]]", file_id="w1:Daily/2026-09-05.md")
    plan = await _note("w1", "Plan.md")

    r = _client(monkeypatch).get(f"/api/v1/files/{daily}/links")
    assert r.status_code == 200, r.text
    assert r.json()["outgoing"][0]["file_id"] == plan


async def test_links_applies_the_pocket_membership_rule(monkeypatch, links_db):
    """A workspace member outside a private pocket cannot read its files' links.

    The read resolves links in the FILE's own pocket scope, so without this gate
    ``outgoing[].filename`` and every backlink name a row the caller cannot
    list. Mutation: drop the ``can_read_pocket`` check in
    ``UnifiedFilesService.file_links`` and the deny case returns 200 with
    "Secret Plan.md" in the body.
    """
    budget = await _note("w1", "Budget.md", "[[Secret Plan]]", pocket_id="p1")
    await _note("w1", "Secret Plan.md", "[[Budget]]", pocket_id="p1")

    async def deny(**_kw):
        return False

    r = _client(monkeypatch, member=deny).get(f"/api/v1/files/{budget}/links")
    assert r.status_code == 404, r.text
    assert "Secret Plan" not in r.text

    async def allow(**_kw):
        return True

    body = _client(monkeypatch, member=allow).get(f"/api/v1/files/{budget}/links").json()
    assert body["outgoing"][0]["filename"] == "Secret Plan.md"
    assert [b["filename"] for b in body["backlinks"]] == ["Secret Plan.md"]


async def test_links_on_a_workspace_file_needs_no_pocket_membership(monkeypatch, links_db):
    """The gate keys on the ROW's pocket, so workspace-only files are unaffected."""
    a = await _note("w1", "A.md", "[[B]]")
    await _note("w1", "B.md")

    async def deny(**_kw):
        return False

    r = _client(monkeypatch, member=deny).get(f"/api/v1/files/{a}/links")
    assert r.status_code == 200, r.text
    assert r.json()["outgoing"][0]["filename"] == "B.md"
