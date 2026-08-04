# test_knowledge_wiki_api.py — Integration tests for the living-wiki API.
# Created: 2026-08-04 — Covers the /knowledge REST surface the wiki frontend
# rebuild codes against: enriched GET /articles rows, GET /articles/{id}
# (full body + orphan fallback + 404), GET /stats, GET /uploads, and the two
# reingest routes. kb output is faked at the subprocess boundary (the spy
# pattern from tests/cloud/kb/test_router_ingest_funnel.py) so the REAL
# KnowledgeService funnel runs and the reingest argv stays pinned; the wiki
# frontmatter / raw-doc reads hit a real temp KB home.
"""Integration tests for the workspace living-wiki API."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pocketpaw_ee.cloud.kb.knowledge_router as knowledge_router_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.agents import knowledge
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license

WORKSPACE = "ws-alpha"
WS_SCOPE = f"workspace:{WORKSPACE}"
WS_DIR = f"workspace_{WORKSPACE.replace('-', '-')}"  # sanitize() keeps '-'


class _SubprocessSpy:
    """Records kb argv + stdin; answers via a matcher on the subcommand."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, tuple[int, str, str]] = {}
        self._real_run = subprocess.run

    def __call__(self, cmd, input=None, timeout=None, **kwargs):  # noqa: A002
        if not cmd or cmd[0] != knowledge.KB_BIN:
            return self._real_run(cmd, input=input, timeout=timeout, **kwargs)
        self.calls.append({"cmd": list(cmd), "input": input, "timeout": timeout})
        subcommand = cmd[1]
        returncode, stdout, stderr = self.responses.get(subcommand, (1, "", "no fake response"))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _write_article(kb_home: str, scope_dir: str, article_id: str, fm: dict, content: str) -> None:
    wiki = os.path.join(kb_home, scope_dir, "wiki")
    os.makedirs(wiki, exist_ok=True)
    with open(os.path.join(wiki, f"{article_id}.md"), "w", encoding="utf-8") as fh:
        fh.write(f"---\n{json.dumps(fm)}\n---\n\n{content}")


def _write_raw_doc(kb_home: str, scope_dir: str, raw_id: str, doc: dict) -> None:
    raw = os.path.join(kb_home, scope_dir, "raw")
    os.makedirs(raw, exist_ok=True)
    with open(os.path.join(raw, f"{raw_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


@pytest.fixture()
def kb_home(tmp_path, monkeypatch) -> str:
    home = str(tmp_path / "knowledge-base")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setattr(knowledge_router_module, "KB_HOME", home)
    return home


@pytest.fixture()
def spy(monkeypatch) -> _SubprocessSpy:
    spy = _SubprocessSpy()
    monkeypatch.setattr(knowledge.subprocess, "run", spy)
    return spy


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    async def fake_list_workspace_agent_ids(workspace_id: str) -> list[str]:
        return ["agent-1"] if workspace_id == WORKSPACE else []

    monkeypatch.setattr(
        knowledge_router_module,
        "_list_workspace_agent_ids",
        fake_list_workspace_agent_ids,
    )

    # Scope-override allowlist: the caller may use the workspace + its agent.
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.kb.service._candidate_scopes",
        AsyncMock(return_value=[WS_SCOPE, "agent:agent-1"]),
    )

    # Stub RBAC (see test_knowledge_router.py for why this seam works here).
    from pocketpaw_ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", lambda *a, **k: None)

    fake_user = SimpleNamespace(
        id="user-1",
        active_workspace=WORKSPACE,
        workspaces=[SimpleNamespace(workspace=WORKSPACE, role="owner")],
    )

    async def fake_current_active_user():
        return fake_user

    from pocketpaw_ee.cloud._core.http import add_error_handler

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = fake_current_active_user
    app.include_router(knowledge_router_module.router, prefix="/api/v1")
    return TestClient(app)


class _FakeMongoFileStore:
    """Async-iterable stand-in for MongoFileStore (Beanie isn't initialised)."""

    rows: list[dict] = []
    doc: SimpleNamespace | None = None
    kb_article_calls: list[dict] = []

    async def iter_by_workspace(self, workspace: str, **kwargs):
        for row in type(self).rows:
            yield row

    async def get_doc_scoped(self, file_id: str, workspace: str):
        doc = type(self).doc
        if doc is not None and doc.file_id == file_id:
            return doc
        return None

    async def set_kb_article(self, file_id, workspace, *, article_id, scope):
        type(self).kb_article_calls.append(
            {"file_id": file_id, "workspace": workspace, "article_id": article_id, "scope": scope}
        )
        return SimpleNamespace()


@pytest.fixture()
def fake_store(monkeypatch) -> type[_FakeMongoFileStore]:
    _FakeMongoFileStore.rows = []
    _FakeMongoFileStore.doc = None
    _FakeMongoFileStore.kb_article_calls = []
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.uploads.mongo_store.MongoFileStore", _FakeMongoFileStore
    )
    return _FakeMongoFileStore


# ---------------------------------------------------------------------------
# GET /knowledge/articles — enriched rows
# ---------------------------------------------------------------------------


def test_articles_rows_carry_wiki_metadata(client, kb_home, spy) -> None:
    spy.responses["list"] = (
        0,
        json.dumps(
            [
                {
                    "id": "art-1",
                    "title": "Deploy runbook",
                    "summary": "How we deploy.",
                    "word_count": 250,
                    "compiled_with": "claude-haiku-4-5",
                    "version": 3,
                }
            ]
        ),
        "",
    )
    _write_article(
        kb_home,
        WS_DIR,
        "art-1",
        {
            "title": "Deploy runbook",
            "categories": ["Ops"],
            "concepts": ["deploys", "rollbacks"],
            "compiled_at": "2026-08-01T12:00:00Z",
            "source_docs": ["raw-1"],
        },
        "# Deploy runbook",
    )
    _write_raw_doc(
        kb_home,
        WS_DIR,
        "raw-orphan",
        {"source": "notes.txt", "raw_text": "some notes", "word_count": 2},
    )

    response = client.get("/api/v1/knowledge/articles")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"articles", "total", "agent_ids"}
    rows = {a["id"]: a for a in body["articles"]}

    # kb list rows appear once per scope (workspace + agent-1 both answered
    # by the same fake) — check the workspace one.
    art = next(a for a in body["articles"] if a["id"] == "art-1" and a["scope"] == WS_SCOPE)
    assert art["word_count"] == 250
    assert art["compiled_with"] == "claude-haiku-4-5"
    assert art["version"] == 3
    assert art["summary"] == "How we deploy."
    assert art["categories"] == ["Ops"]
    assert art["concepts"] == ["deploys", "rollbacks"]
    assert art["compiled_at"] == "2026-08-01T12:00:00Z"
    # No updated_at from kb list — falls back to compiled_at.
    assert art["updated_at"] == "2026-08-01T12:00:00Z"

    # The orphan raw doc surfaces as a synthetic uncompiled row.
    orphan = rows["raw-orphan"]
    assert orphan["title"] == "notes.txt"
    assert orphan["compiled_with"] is None
    assert orphan["word_count"] == 2


# ---------------------------------------------------------------------------
# GET /knowledge/articles/{article_id}
# ---------------------------------------------------------------------------


def test_get_article_full_body(client, kb_home, spy) -> None:
    spy.responses["show"] = (
        0,
        json.dumps(
            {
                "id": "art-1",
                "title": "Deploy runbook",
                "summary": "How we deploy.",
                "content": "# Deploy runbook\n\nSteps.",
                "concepts": ["deploys"],
                "categories": ["Ops"],
                "backlinks": ["art-2"],
                "word_count": 250,
                "compiled_with": "claude-haiku-4-5",
                "version": 3,
            }
        ),
        "",
    )
    _write_article(
        kb_home,
        WS_DIR,
        "art-1",
        {"compiled_at": "2026-08-01T12:00:00Z", "source_docs": ["raw-1"]},
        "# Deploy runbook",
    )

    response = client.get("/api/v1/knowledge/articles/art-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"] == "# Deploy runbook\n\nSteps."
    assert body["backlinks"] == ["art-2"]
    assert body["compiled_at"] == "2026-08-01T12:00:00Z"
    assert body["source_docs"] == ["raw-1"]
    assert body["scope"] == WS_SCOPE
    assert body["orphan"] is False
    # kb show must have been asked for exactly this article + scope.
    assert spy.calls[0]["cmd"][1:] == ["show", "art-1", "--scope", WS_SCOPE, "--json"]


def test_get_article_unknown_id_404(client, kb_home, spy) -> None:
    # kb show exits 1 ("Article not found") and there is no raw doc either.
    response = client.get("/api/v1/knowledge/articles/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "article.not_found"


def test_get_article_orphan_raw_doc_fallback(client, kb_home, spy) -> None:
    _write_raw_doc(
        kb_home,
        WS_DIR,
        "raw-orphan",
        {"source": "notes.txt", "raw_text": "some notes", "word_count": 2},
    )
    response = client.get("/api/v1/knowledge/articles/raw-orphan")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["orphan"] is True
    assert body["content"] == "some notes"
    assert body["compiled_with"] is None
    assert body["title"] == "notes.txt"


def test_get_article_scope_guard_denies_foreign_user_scope(client, kb_home, spy) -> None:
    response = client.get("/api/v1/knowledge/articles/art-1?scope=user:someone-else")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "kb.scope_forbidden"
    assert spy.calls == []  # denied before any kb call


# ---------------------------------------------------------------------------
# GET /knowledge/stats
# ---------------------------------------------------------------------------


def test_stats_rolls_up_workspace_and_agent_scopes(client, kb_home, spy) -> None:
    spy.responses["stats"] = (
        0,
        json.dumps(
            {
                "scope": "x",
                "articles": 4,
                "raw_docs": 5,
                "words": 1000,
                "concepts": 12,
                "categories": 3,
                "vectors": 0,
            }
        ),
        "",
    )
    response = client.get("/api/v1/knowledge/stats")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_ids"] == ["agent-1"]
    assert [r["scope"] for r in body["stats"]] == [WS_SCOPE, "agent:agent-1"]
    ws_row = body["stats"][0]
    assert ws_row == {
        "scope": WS_SCOPE,
        "agent_id": None,
        "articles": 4,
        "words": 1000,
        "raw_docs": 5,
        "concepts": 12,
        "categories": 3,
    }
    # One kb stats call per scope.
    assert [c["cmd"][1] for c in spy.calls] == ["stats", "stats"]


# ---------------------------------------------------------------------------
# POST /knowledge/reingest
# ---------------------------------------------------------------------------


def test_reingest_routes_raw_doc_through_funnel(client, kb_home, spy, monkeypatch) -> None:
    """The reingest argv is pinned to the funnel's plain-ingest shape."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_article(
        kb_home,
        WS_DIR,
        "art-1",
        {"title": "Notes", "source_docs": ["raw-1"]},
        "# Notes",
    )
    _write_raw_doc(
        kb_home,
        WS_DIR,
        "raw-1",
        {"source": "notes.txt", "raw_text": "the raw text", "word_count": 3},
    )
    spy.responses["ingest"] = (0, json.dumps({"id": "art-1", "compiled_with": "llm"}), "")

    response = client.post(
        "/api/v1/knowledge/reingest", json={"article_id": "art-1", "scope": WS_SCOPE}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == WS_SCOPE
    assert body["raw_doc_id"] == "raw-1"
    assert body["source"] == "notes.txt"
    assert body["result"]["id"] == "art-1"

    assert len(spy.calls) == 1
    assert spy.calls[0]["cmd"][1:] == [
        "ingest",
        "--scope",
        WS_SCOPE,
        "--source",
        "notes.txt",
        "--json",
    ]
    assert spy.calls[0]["input"] == "the raw text"


def test_reingest_orphan_raw_id_directly(client, kb_home, spy, monkeypatch) -> None:
    """An orphan raw-doc id (no wiki article) reingests its own raw doc."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _write_raw_doc(
        kb_home,
        WS_DIR,
        "raw-orphan",
        {"source": "orphan.txt", "raw_text": "orphan text", "word_count": 2},
    )
    spy.responses["ingest"] = (0, json.dumps({"id": "art-new", "compiled_with": "llm"}), "")

    response = client.post("/api/v1/knowledge/reingest", json={"article_id": "raw-orphan"})
    assert response.status_code == 200, response.text
    assert response.json()["raw_doc_id"] == "raw-orphan"
    assert spy.calls[0]["input"] == "orphan text"


def test_reingest_unknown_article_404(client, kb_home, spy) -> None:
    response = client.post("/api/v1/knowledge/reingest", json={"article_id": "missing"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "article.not_found"
    assert spy.calls == []


def test_reingest_scope_guard_denies_foreign_user_scope(client, kb_home, spy) -> None:
    response = client.post(
        "/api/v1/knowledge/reingest",
        json={"article_id": "art-1", "scope": "user:someone-else"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "kb.scope_forbidden"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# POST /knowledge/reingest-upload
# ---------------------------------------------------------------------------


def _install_upload_pipeline(monkeypatch, tmp_path, *, text: str) -> None:
    """Fake adapter + extraction chain around a real temp file."""
    blob = tmp_path / "report.pdf"
    blob.write_bytes(b"%PDF-fake")

    adapter = SimpleNamespace(local_path=lambda key: blob)
    monkeypatch.setattr(knowledge_router_module, "_resolve_upload_adapter", lambda: adapter)

    class _FakeChain:
        async def run(self, path, mime):
            assert str(path) == str(blob)
            return SimpleNamespace(text=text)

    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: _FakeChain())


def test_reingest_upload_extracts_and_funnels(
    client, kb_home, spy, fake_store, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake_store.doc = SimpleNamespace(
        file_id="up-1",
        storage_key="ws/up-1",
        filename="report.pdf",
        mime="application/pdf",
        hide_from_ai=False,
    )
    _install_upload_pipeline(monkeypatch, tmp_path, text="extracted report text")
    spy.responses["ingest"] = (0, json.dumps({"id": "art-up", "compiled_with": "llm"}), "")

    response = client.post(
        "/api/v1/knowledge/reingest-upload", json={"upload_id": "up-1", "scope": WS_SCOPE}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "report.pdf"
    assert body["result"]["id"] == "art-up"

    # The funnel argv carries the ORIGINAL filename as source.
    assert spy.calls[0]["cmd"][1:] == [
        "ingest",
        "--scope",
        WS_SCOPE,
        "--source",
        "report.pdf",
        "--json",
    ]
    assert spy.calls[0]["input"] == "extracted report text"

    # FL-11b tracking recorded for the later hide-from-AI purge.
    assert fake_store.kb_article_calls == [
        {"file_id": "up-1", "workspace": WORKSPACE, "article_id": "art-up", "scope": WS_SCOPE}
    ]


def test_reingest_upload_hidden_file_refused(client, kb_home, spy, fake_store) -> None:
    fake_store.doc = SimpleNamespace(
        file_id="up-hidden",
        storage_key="ws/up-hidden",
        filename="secret.pdf",
        mime="application/pdf",
        hide_from_ai=True,
    )
    response = client.post("/api/v1/knowledge/reingest-upload", json={"upload_id": "up-hidden"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "knowledge.upload_hidden"
    assert spy.calls == []


def test_reingest_upload_unknown_id_404(client, kb_home, spy, fake_store) -> None:
    response = client.post("/api/v1/knowledge/reingest-upload", json={"upload_id": "nope"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "upload.not_found"


# ---------------------------------------------------------------------------
# GET /knowledge/uploads
# ---------------------------------------------------------------------------


def test_list_uploads_has_article_markers(client, kb_home, fake_store) -> None:
    uploaded_at = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    fake_store.rows = [
        {
            "file_id": "up-tracked",
            "filename": "tracked.pdf",
            "mime": "application/pdf",
            "size": 100,
            "created_at": uploaded_at,
            "hide_from_ai": False,
            "kb_article_id": "art-1",
            "kb_scope": WS_SCOPE,
        },
        {
            "file_id": "up-legacy",
            "filename": "legacy.txt",
            "mime": "text/plain",
            "size": 50,
            "created_at": uploaded_at,
            "hide_from_ai": False,
            "kb_article_id": None,
            "kb_scope": None,
        },
        {
            "file_id": "up-pending",
            "filename": "pending.docx",
            "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size": 75,
            "created_at": uploaded_at,
            "hide_from_ai": False,
            "kb_article_id": None,
            "kb_scope": None,
        },
        {
            "file_id": "up-hidden",
            "filename": "hidden.pdf",
            "mime": "application/pdf",
            "size": 10,
            "created_at": uploaded_at,
            "hide_from_ai": True,
            "kb_article_id": None,
            "kb_scope": None,
        },
    ]
    # legacy.txt predates FL-11b tracking but IS compiled in the scope —
    # article frontmatter → raw doc → source filename match.
    _write_article(
        kb_home, WS_DIR, "art-legacy", {"title": "Legacy", "source_docs": ["raw-legacy"]}, "# L"
    )
    _write_raw_doc(
        kb_home, WS_DIR, "raw-legacy", {"source": "legacy.txt", "raw_text": "x", "word_count": 1}
    )

    response = client.get("/api/v1/knowledge/uploads")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == WS_SCOPE
    rows = {u["id"]: u for u in body["uploads"]}
    assert set(rows) == {"up-tracked", "up-legacy", "up-pending"}  # hidden excluded
    assert rows["up-tracked"]["has_article"] is True
    assert rows["up-legacy"]["has_article"] is True  # filename fallback
    assert rows["up-pending"]["has_article"] is False
    assert rows["up-tracked"]["uploaded_at"] == uploaded_at.isoformat()
    assert rows["up-tracked"]["size"] == 100
