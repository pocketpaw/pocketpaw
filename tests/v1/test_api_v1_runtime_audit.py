# test_api_v1_runtime_audit.py — Integration tests for /runtime/audit.
# Created: 2026-04-19 (Cluster C / PR4) — Exercises the new canonical
# audit surface + the legacy /audit alias. Covers the workspace_id rollup,
# full-text q filter, and the Deprecation header on the legacy path.

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.deps import require_scope
from pocketpaw.audit import router as legacy_router_module
from pocketpaw.audit import runtime_router as runtime_router_module
from pocketpaw.audit.store import AuditStore, get_audit_store


@pytest.fixture()
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


@pytest.fixture()
def client(store: AuditStore) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_audit_store] = lambda: store
    # Disable the audit scope guard on both routers so we can hit the
    # forwarding + canonical paths without plumbing an auth token. Both
    # the legacy and the new router require "audit" scope in production.
    # The existing /audit endpoint and the new /runtime/audit share the
    # same guard factory output, so a single override covers both.
    app.dependency_overrides[require_scope("audit")] = lambda: None
    app.include_router(legacy_router_module.router, prefix="/api/v1")
    app.include_router(runtime_router_module.router, prefix="/api/v1")
    return TestClient(app)


@pytest.fixture()
async def seed(store: AuditStore):
    await store.log_entry(
        actor="user:alice",
        action="agent_query",
        category="decision",
        description="Alice asked sales-bot for the pipeline",
        pocket_id="p-alpha-sales",
        context={"workspace_id": "ws-alpha", "query": "pipeline snapshot"},
    )
    await store.log_entry(
        actor="user:alice",
        action="kb_ingest",
        category="data",
        description="Onboarding guide uploaded",
        pocket_id="p-alpha-sales",
        context={"workspace_id": "ws-alpha"},
    )
    await store.log_entry(
        actor="user:carol",
        action="agent_query",
        category="decision",
        description="Carol asked the support bot about SLAs",
        pocket_id="p-beta-support",
        context={"workspace_id": "ws-beta", "query": "SLA review"},
    )


@pytest.mark.asyncio
async def test_runtime_audit_workspace_rollup(client: TestClient, seed) -> None:
    resp = client.get("/api/v1/runtime/audit?workspace_id=ws-alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    for entry in body["entries"]:
        assert entry["context"]["workspace_id"] == "ws-alpha"


@pytest.mark.asyncio
async def test_runtime_audit_full_text_search(client: TestClient, seed) -> None:
    resp = client.get("/api/v1/runtime/audit?q=pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert "pipeline" in (
        body["entries"][0]["description"].lower() + str(body["entries"][0]["context"]).lower()
    )


@pytest.mark.asyncio
async def test_runtime_audit_q_length_cap(client: TestClient, seed) -> None:
    # 201 chars — pydantic Query(max_length=200) rejects to prevent a
    # pathological LIKE query exhausting the CPU.
    long_q = "x" * 201
    resp = client.get(f"/api/v1/runtime/audit?q={long_q}")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_legacy_audit_forwards_and_deprecation_header(client: TestClient, seed) -> None:
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 200
    # Legacy path surfaces same rows (no ws filter, no q).
    assert resp.json()["total"] == 3
    # And flags itself deprecated.
    assert resp.headers.get("deprecation") == "true"
    assert "runtime/audit" in resp.headers.get("link", "")


@pytest.mark.asyncio
async def test_runtime_audit_export_csv(client: TestClient, seed) -> None:
    resp = client.get("/api/v1/runtime/audit/export?format=csv&workspace_id=ws-alpha")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    # Two rows + header → at least 3 lines.
    assert resp.content.decode().count("\n") >= 3


@pytest.mark.asyncio
async def test_runtime_audit_rejects_injection_q(client: TestClient, seed) -> None:
    resp = client.get("/api/v1/runtime/audit?q='; DROP TABLE audit_log; --")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # And the seeded rows are still there — the injection did not
    # corrupt the table.
    all_rows = client.get("/api/v1/runtime/audit").json()
    assert all_rows["total"] == 3
