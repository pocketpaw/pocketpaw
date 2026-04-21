# test_knowledge_router.py — Integration tests for /api/v1/knowledge/articles.
# Created: 2026-04-19 (Cluster C / PR1) — Exercises the workspace KB browser
# route with a User stub that already owns the active workspace, so the
# ``kb.read`` action guard resolves without a real RBAC database. Proves the
# merge, filter, and cross-workspace block contracts end-to-end through
# FastAPI.
"""Integration tests for the workspace-level knowledge router."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ee.cloud.kb.knowledge_router as knowledge_router_module
from ee.cloud.auth import current_active_user
from ee.cloud.license import require_license


@pytest.fixture()
def client(monkeypatch):
    fake_rows = {
        "workspace:ws-alpha": [
            {"id": "ws-doc-1", "title": "Workspace KB 1", "updated_at": "2026-04-18T10:00:00Z"},
            {"id": "ws-doc-2", "title": "Workspace KB 2", "updated_at": "2026-04-19T10:00:00Z"},
        ],
        "agent:agent-1": [
            {"id": "a1-doc", "title": "Agent 1 KB", "updated_at": "2026-04-17T10:00:00Z"},
        ],
        "agent:agent-2": [
            {"id": "a2-doc", "title": "Agent 2 KB", "updated_at": "2026-04-16T10:00:00Z"},
        ],
    }

    def fake_kb_list(scope: str):
        return fake_rows.get(scope, [])

    async def fake_list_workspace_agent_ids(workspace_id: str) -> list[str]:
        if workspace_id == "ws-alpha":
            return ["agent-1", "agent-2"]
        return []

    monkeypatch.setattr(knowledge_router_module, "_call_kb_list", fake_kb_list)
    monkeypatch.setattr(
        knowledge_router_module,
        "_list_workspace_agent_ids",
        fake_list_workspace_agent_ids,
    )

    # Stub RBAC: make every action check pass for our fake user.
    from pocketpaw.ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", lambda *a, **k: None)

    # Build a fake user that already has ws-alpha as its active workspace.
    fake_user = SimpleNamespace(
        id="user-1",
        active_workspace="ws-alpha",
        workspaces=[SimpleNamespace(workspace="ws-alpha", role="owner")],
    )

    async def fake_current_active_user():
        return fake_user

    app = FastAPI()
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = fake_current_active_user
    app.include_router(knowledge_router_module.router, prefix="/api/v1")
    return TestClient(app)


def test_list_workspace_articles_unions_scopes(client: TestClient) -> None:
    response = client.get("/api/v1/knowledge/articles")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    ids = {a["id"] for a in body["articles"]}
    assert ids == {"ws-doc-1", "ws-doc-2", "a1-doc", "a2-doc"}
    scopes = {a["scope"] for a in body["articles"]}
    assert scopes == {"workspace:ws-alpha", "agent:agent-1", "agent:agent-2"}
    assert set(body["agent_ids"]) == {"agent-1", "agent-2"}


def test_filter_by_workspace_keyword(client: TestClient) -> None:
    response = client.get("/api/v1/knowledge/articles?agent_id=workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(a["scope"] == "workspace:ws-alpha" for a in body["articles"])


def test_filter_by_agent_id(client: TestClient) -> None:
    response = client.get("/api/v1/knowledge/articles?agent_id=agent-1")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["articles"][0]["agent_id"] == "agent-1"


def test_cross_workspace_id_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/knowledge/articles?workspace_id=ws-beta")
    assert response.status_code == 403
    assert "must match" in response.json()["detail"]


def test_unknown_agent_returns_empty_not_leak(client: TestClient) -> None:
    """An agent id outside the workspace must NOT cross-query another tenant.

    The route returns an empty list + the real agent_ids, not a 404 — so the
    caller can't probe existence of agents in a different workspace.
    """
    response = client.get("/api/v1/knowledge/articles?agent_id=some-other-agent")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["articles"] == []
    assert set(body["agent_ids"]) == {"agent-1", "agent-2"}
