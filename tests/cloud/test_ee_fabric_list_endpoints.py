# test_ee_fabric_list_endpoints.py — Integration tests for /fabric/objects
# and /fabric/links list endpoints.
# Created: 2026-04-19 (Cluster C / PR3) — Wires the new list endpoints added
# alongside PocketDataPanel's Objects/Links sub-tab wire-up. Exercises the
# store + router round-trip so the frontend contract holds without a live
# SQLite db.
# Updated: 2026-05-07 (fix/test-fixtures-auth-context) — seed auth context in
#   the client fixture so route tests pass against the workspace RBAC guards
#   added in #1059 and the require_plan_feature("fabric") gate added in #1060.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ee.fabric.router as fabric_router_module
from ee.cloud._core.deps import current_workspace_id
from ee.cloud.auth import current_active_user
from ee.cloud.license import require_license
from ee.fabric.store import FabricStore


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Member of the test workspace — fabric.read/write are MEMBER-tier."""

    def __init__(self, workspace_id: str = "ws-test") -> None:
        self.id = "user-test-1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="member")]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """Build an isolated app with the fabric router and seeded auth context.

    Overrides:
      - require_license: no-op so license validation doesn't gate tests.
      - current_active_user: returns a fake member of ws-test.
      - current_workspace_id: returns 'ws-test'.
      - workspace_service.get_workspace_plan: returns 'business' so the
        require_plan_feature('fabric') gate added in #1060 passes (fabric
        is a business+ feature in PLAN_FEATURES).
    """
    # Point the module-level store at the tmp db. The router always calls
    # _store() lazily so setattr is enough.
    test_db = tmp_path / "fabric-test.db"
    monkeypatch.setattr(fabric_router_module, "_DB_PATH", test_db)

    import ee.cloud.workspace.service as ws_svc
    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="business"))

    fake_user = _FakeUser()
    app = FastAPI()
    app.include_router(fabric_router_module.router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: fake_user.active_workspace
    return TestClient(app)


@pytest.fixture()
def seeded_store(tmp_path: Path) -> FabricStore:
    return FabricStore(tmp_path / "fabric-store.db")


@pytest.mark.asyncio
async def test_store_list_links_filters_by_type(seeded_store: FabricStore) -> None:
    t = await seeded_store.define_type(name="Customer", properties=[])
    o1 = await seeded_store.create_object(t.id, {"name": "Alice"})
    o2 = await seeded_store.create_object(t.id, {"name": "Bob"})
    o3 = await seeded_store.create_object(t.id, {"name": "Carol"})
    await seeded_store.link(o1.id, o2.id, "reports_to")
    await seeded_store.link(o2.id, o3.id, "reports_to")
    await seeded_store.link(o1.id, o3.id, "mentors")

    reports, total = await seeded_store.list_links(link_type="reports_to")
    assert total == 2
    assert all(link.link_type == "reports_to" for link in reports)

    from_o1, from_o1_total = await seeded_store.list_links(from_id=o1.id)
    assert from_o1_total == 2
    assert all(link.from_object_id == o1.id for link in from_o1)


@pytest.mark.asyncio
async def test_store_list_links_binds_params_no_injection(
    seeded_store: FabricStore,
) -> None:
    """An attacker-controlled link_type string must not be interpolated.

    SQLite won't execute multi-statement queries via aiosqlite's execute(),
    so the SQL-injection vector is inherently weaker than the FTS case we
    handle in PR4. We still prove by construction that the filter is bound:
    if it were concatenated, the trailing ``'; DROP TABLE`` would either
    error or return silent garbage. With binding it's a string that simply
    matches no rows.
    """
    t = await seeded_store.define_type(name="T", properties=[])
    o1 = await seeded_store.create_object(t.id, {})
    o2 = await seeded_store.create_object(t.id, {})
    await seeded_store.link(o1.id, o2.id, "safe_type")

    evil = "safe_type'; DROP TABLE fabric_links; --"
    links, total = await seeded_store.list_links(link_type=evil)
    assert total == 0
    assert links == []

    # The table still exists.
    all_links, _ = await seeded_store.list_links()
    assert len(all_links) == 1


def test_route_list_objects_returns_envelope(client: TestClient) -> None:
    # Seed via the POST endpoints so the full round-trip is exercised.
    type_resp = client.post(
        "/api/v1/fabric/types",
        json={"name": "Task", "properties": []},
    )
    assert type_resp.status_code == 201, type_resp.text
    type_id = type_resp.json()["id"]

    client.post(
        "/api/v1/fabric/objects",
        json={"type_id": type_id, "properties": {"title": "Write tests"}},
    )
    client.post(
        "/api/v1/fabric/objects",
        json={"type_id": type_id, "properties": {"title": "Review PR"}},
    )

    resp = client.get("/api/v1/fabric/objects")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"objects", "total"}
    assert body["total"] == 2
    assert len(body["objects"]) == 2


def test_route_list_objects_filter_by_type(client: TestClient) -> None:
    t1 = client.post("/api/v1/fabric/types", json={"name": "A", "properties": []}).json()
    t2 = client.post("/api/v1/fabric/types", json={"name": "B", "properties": []}).json()
    client.post("/api/v1/fabric/objects", json={"type_id": t1["id"], "properties": {}})
    client.post("/api/v1/fabric/objects", json={"type_id": t2["id"], "properties": {}})
    client.post("/api/v1/fabric/objects", json={"type_id": t2["id"], "properties": {}})

    resp = client.get(f"/api/v1/fabric/objects?type_id={t2['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert all(o["type_id"] == t2["id"] for o in body["objects"])


def test_route_list_links_returns_envelope(client: TestClient) -> None:
    t = client.post("/api/v1/fabric/types", json={"name": "X", "properties": []}).json()
    o1 = client.post("/api/v1/fabric/objects", json={"type_id": t["id"], "properties": {}}).json()
    o2 = client.post("/api/v1/fabric/objects", json={"type_id": t["id"], "properties": {}}).json()
    client.post(
        "/api/v1/fabric/links",
        json={"from_id": o1["id"], "to_id": o2["id"], "link_type": "related"},
    )

    resp = client.get("/api/v1/fabric/links")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"links", "total"}
    assert body["total"] == 1
    assert body["links"][0]["link_type"] == "related"


def test_route_list_links_rejects_bad_limit(client: TestClient) -> None:
    resp = client.get("/api/v1/fabric/links?limit=0")
    assert resp.status_code == 422
    resp = client.get("/api/v1/fabric/links?limit=100000")
    assert resp.status_code == 422
