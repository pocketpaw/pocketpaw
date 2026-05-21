# test_tasks_router.py — router-layer tests for the Tasks entity.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend.
#   Asserts status-code mapping, request body validation, and tenant
#   isolation at the HTTP boundary. Uses the shared ``mongo_db`` fixture
#   plus a per-test FastAPI app with auth deps overridden.
"""Router smoke tests for the Tasks entity."""

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.tasks.router import router


@pytest_asyncio.fixture
async def app_client(mongo_db) -> AsyncClient:
    from pocketpaw_ee.cloud.auth import current_active_user

    class _U:
        id = "creator-1"
        active_workspace = "w1"
        workspaces: list = []

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: _U()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _create_body(**overrides) -> dict:
    body = {
        "title": "Draft run-of-show",
        "summary": "for May 23 wedding",
        "assignee": {"kind": "agent", "id": "agent-events", "name": "events-agent"},
        "priority": "normal",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_returns_201_equivalent_200_with_dto(app_client) -> None:
    resp = await app_client.post("/api/v1/tasks", json=_create_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Draft run-of-show"
    assert body["assignee"]["id"] == "agent-events"
    assert body["status"] == "proposed"
    assert body["workspace_id"] == "w1"
    assert body["creator_id"] == "creator-1"
    assert body["created_at"].endswith("+00:00")


async def test_create_invalid_assignee_kind_returns_422(app_client) -> None:
    payload = _create_body(assignee={"kind": "robot", "id": "x"})
    resp = await app_client.post("/api/v1/tasks", json=payload)
    assert resp.status_code == 422


async def test_create_missing_title_returns_422(app_client) -> None:
    payload = _create_body()
    payload.pop("title")
    resp = await app_client.post("/api/v1/tasks", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List + Get
# ---------------------------------------------------------------------------


async def test_list_returns_tasks(app_client) -> None:
    await app_client.post("/api/v1/tasks", json=_create_body(title="t1"))
    await app_client.post("/api/v1/tasks", json=_create_body(title="t2"))
    resp = await app_client.get("/api/v1/tasks")
    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert titles == {"t1", "t2"}


async def test_list_filters_assignee_via_query(app_client) -> None:
    await app_client.post(
        "/api/v1/tasks",
        json=_create_body(
            title="for-a",
            assignee={"kind": "agent", "id": "agent-a", "name": "a"},
        ),
    )
    await app_client.post(
        "/api/v1/tasks",
        json=_create_body(
            title="for-b",
            assignee={"kind": "agent", "id": "agent-b", "name": "b"},
        ),
    )
    resp = await app_client.get("/api/v1/tasks?assignee=agent-a")
    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert titles == {"for-a"}


async def test_get_returns_404_for_missing(app_client) -> None:
    resp = await app_client.get("/api/v1/tasks/507f1f77bcf86cd799439011")
    assert resp.status_code == 404


async def test_get_returns_404_for_malformed_id(app_client) -> None:
    resp = await app_client.get("/api/v1/tasks/not-an-objectid")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_patch_updates_title(app_client) -> None:
    created = (await app_client.post("/api/v1/tasks", json=_create_body())).json()
    resp = await app_client.patch(f"/api/v1/tasks/{created['id']}", json={"title": "new title"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "new title"


# ---------------------------------------------------------------------------
# State-machine verbs
# ---------------------------------------------------------------------------


async def test_claim_succeeds_when_assigned(app_client) -> None:
    created = (await app_client.post("/api/v1/tasks", json=_create_body())).json()
    resp = await app_client.post(
        f"/api/v1/tasks/{created['id']}/claim", json={"agent_id": "agent-events"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["task"]["status"] == "in_progress"


async def test_claim_returns_ok_false_for_other_agent(app_client) -> None:
    created = (await app_client.post("/api/v1/tasks", json=_create_body())).json()
    resp = await app_client.post(
        f"/api/v1/tasks/{created['id']}/claim", json={"agent_id": "agent-not-mine"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "not_assigned_to_agent"


async def test_complete_archive_returns_done(app_client) -> None:
    created = (await app_client.post("/api/v1/tasks", json=_create_body())).json()
    # Claim first so it's in_progress
    await app_client.post(f"/api/v1/tasks/{created['id']}/claim", json={"agent_id": "agent-events"})
    resp = await app_client.post(
        f"/api/v1/tasks/{created['id']}/complete", json={"next_action": "archive"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


async def test_block_returns_blocked_with_reason(app_client) -> None:
    created = (await app_client.post("/api/v1/tasks", json=_create_body())).json()
    resp = await app_client.post(
        f"/api/v1/tasks/{created['id']}/block",
        json={"reason": "needs vendor confirm"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "needs vendor confirm"


async def test_reassign_flips_assignee(app_client) -> None:
    created = (await app_client.post("/api/v1/tasks", json=_create_body())).json()
    resp = await app_client.post(
        f"/api/v1/tasks/{created['id']}/reassign",
        json={
            "assignee_kind": "human",
            "assignee_id": "u-jess",
            "assignee_name": "Jess",
        },
    )
    assert resp.status_code == 200
    assignee = resp.json()["assignee"]
    assert assignee["kind"] == "human"
    assert assignee["id"] == "u-jess"
