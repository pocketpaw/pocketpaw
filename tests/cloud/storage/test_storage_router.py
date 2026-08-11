# tests/cloud/storage/test_storage_router.py — GET /storage/usage read surface
# (feat/billing-storage-caps).
#
# Thin HTTP proof of the storage read: the route resolves the CALLER's workspace
# (via the ``current_workspace_id`` dep) and returns used_bytes / max_bytes /
# remaining_bytes / percent_used from real Workspace + FileUpload docs. License
# is waived and the workspace pinned to the seeded id. The seeding is async
# (mongo_db), so the whole test is async; the TestClient call itself is sync.
#
# The READ is NOT gated on ``billing_enforced`` (it is informational — a Go
# workspace shows "15 GB" whether or not enforcement is on), so no billing patch
# is needed here; test_get_storage_usage_reports_used_vs_cap_billing_off pins the
# exact dev/OSS regression where a Go workspace read "Unlimited".
#
# Created 2026-08-08 (feat/billing-storage-caps).

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_workspace_id
from pocketpaw_ee.cloud.storage.router import router as storage_router


async def _seed_workspace_with_file(plan: str, size: int) -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}", owner="u-owner", plan=plan)
    await ws.insert()
    ws_id = str(ws.id)
    await FileUpload(
        file_id=uuid.uuid4().hex,
        storage_key=f"u/{uuid.uuid4().hex}",
        filename="doc.pdf",
        mime="application/pdf",
        size=size,
        workspace=ws_id,
        owner="u-owner",
    ).insert()
    return ws_id


def _client_for(ws_id: str) -> TestClient:
    app = FastAPI()
    app.include_router(storage_router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: ws_id
    return TestClient(app, raise_server_exceptions=False)


async def test_get_storage_usage_reports_used_vs_cap(mongo_db) -> None:
    ws_id = await _seed_workspace_with_file(plan="go", size=1_500_000_000)

    resp = _client_for(ws_id).get("/storage/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == ws_id
    assert body["used_bytes"] == 1_500_000_000
    assert body["max_bytes"] == 15_000_000_000
    assert body["remaining_bytes"] == 13_500_000_000
    assert body["percent_used"] == 10.0


async def test_get_storage_usage_reports_used_vs_cap_billing_off(mongo_db, monkeypatch) -> None:
    """billing off (OSS/dev, the default) → a Go workspace STILL reads 15 GB.

    Regression for the live bug where the storage page showed "Unlimited" on a Go
    plan: the read used to short-circuit to a None cap when ``billing_enforced``
    was off (its default). The read is informational — only the upload GATE is
    gated on billing.
    """
    from types import SimpleNamespace

    import pocketpaw.config as ppconfig

    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=False, dodo_plan_products=None),
    )
    ws_id = await _seed_workspace_with_file(plan="go", size=1_500_000_000)

    resp = _client_for(ws_id).get("/storage/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_bytes"] == 1_500_000_000
    assert body["max_bytes"] == 15_000_000_000
    assert body["remaining_bytes"] == 13_500_000_000
    assert body["percent_used"] == 10.0


async def test_get_storage_usage_enterprise_is_uncapped(mongo_db) -> None:
    ws_id = await _seed_workspace_with_file(plan="enterprise", size=10**12)

    resp = _client_for(ws_id).get("/storage/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_bytes"] == 10**12
    assert body["max_bytes"] is None
    assert body["remaining_bytes"] is None
    assert body["percent_used"] is None


async def test_get_storage_usage_empty_workspace_is_zero(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="Acme", slug="acme-empty", owner="u-owner", plan="free")
    await ws.insert()

    resp = _client_for(str(ws.id)).get("/storage/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["used_bytes"] == 0
    assert body["max_bytes"] == 5_000_000_000
    assert body["percent_used"] == 0.0
