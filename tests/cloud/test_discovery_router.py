# test_discovery_router.py — contract tests for the workspace-discovery TRIGGER.
# Created: 2026-06-21 (SZD finish slice F1 / feat/szd-finish-core) — pins the
#   POST /cloud/discovery/run front door: (1) only ENABLED connectors reach the
#   orchestrator, (2) 202 + run_id on success, (3) the connector.execute action
#   gate denies with 403, (4) the workspace resolves from the active workspace
#   (current_workspace_id) — there is NO {workspace_id} path param.
#
# Pattern mirrors test_knowledge_router.py: override current_active_user with a
# fake User that owns the active workspace, patch the RBAC check the gate
# consumes (pocketpaw_ee.cloud._core.deps.check_workspace_action — imported
# by-name there, so the consumer is the patch target), and patch
# run_discovery_and_propose so the test never samples real connectors.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.discovery.router import router as discovery_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.discovery.orchestrate import DiscoveryProposalResult

_WS = "ws-a"
_USER = "u-a"


def _fake_result(run_id: str = "orch-run-1") -> DiscoveryProposalResult:
    return DiscoveryProposalResult(
        run_id=run_id,
        fabric_objects_action_id="fa-1",
        pocket_action_id="pa-1",
        materialised_types=["Invoice"],
        skipped_types={},
        superseded_action_ids=[],
        instinct_action_ids=["ir-1"],
    )


def _build_app(monkeypatch, *, allow: bool = True) -> FastAPI:
    """Mount the discovery router with auth + license overridden.

    ``allow`` toggles the RBAC gate: when False the connector.execute check
    raises GuardForbidden, exercising the 403 deny path.
    """
    from pocketpaw_ee.cloud._core import deps as core_deps
    from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

    if allow:
        monkeypatch.setattr(core_deps, "check_workspace_action", lambda *a, **k: None)
    else:

        def _deny(*_a, **_k):
            raise GuardForbidden("connector.execute_denied", "Access denied")

        monkeypatch.setattr(core_deps, "check_workspace_action", _deny)

    fake_user = SimpleNamespace(
        id=_USER,
        active_workspace=_WS,
        workspaces=[SimpleNamespace(workspace=_WS, role="owner")],
    )

    async def _fake_current_active_user():
        return fake_user

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _fake_current_active_user
    app.include_router(discovery_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def client(monkeypatch, mongo_db):  # noqa: ARG001 — mongo_db wires Beanie
    app = _build_app(monkeypatch, allow=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


# ---------------------------------------------------------------------------
# 202 + run_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_202_with_run_id(client: AsyncClient, monkeypatch):
    """A permitted caller gets 202 Accepted and an opaque run_id."""
    from pocketpaw_ee.cloud.discovery import service as discovery_service

    fake_orch = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(discovery_service, "run_discovery_and_propose", fake_orch)

    resp = await client.post("/api/v1/cloud/discovery/run", json={})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert isinstance(body["run_id"], str) and body["run_id"]
    # Fire-and-forget: action ids are NOT ready synchronously, returned empty.
    assert body["fabric_objects_action_id"] is None
    assert body["instinct_action_ids"] == []
    # Let the background task drain so the orchestrator was actually invoked.
    await asyncio.sleep(0)
    assert fake_orch.await_count == 1


# ---------------------------------------------------------------------------
# Only ENABLED connectors reach the orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_enumerates_enabled_connectors_only(client: AsyncClient, monkeypatch):
    """Disabled connectors are excluded from the connector_ids passed to the
    orchestrator; enabled ones are included."""
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.discovery import service as discovery_service

    fake_orch = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(discovery_service, "run_discovery_and_propose", fake_orch)

    # Enable exactly one real connector from the catalog in this workspace.
    catalog = await connectors_service.list_connectors(_WS, user_id=_USER)
    assert catalog, "registry catalog should be non-empty"
    enabled_name = catalog[0].name
    from pocketpaw_ee.cloud.connectors.dto import EnableConnectorRequest

    await connectors_service.enable_connector(
        _WS, enabled_name, EnableConnectorRequest(scope="workspace")
    )

    resp = await client.post("/api/v1/cloud/discovery/run", json={})
    assert resp.status_code == 202, resp.text
    await asyncio.sleep(0)  # drain the fire-and-forget task

    assert fake_orch.await_count == 1
    # signature: run_discovery_and_propose(workspace_id, user_id, connector_ids, opts)
    call = fake_orch.await_args
    passed_workspace, passed_user, passed_connector_ids = call.args[0], call.args[1], call.args[2]
    assert passed_workspace == _WS
    assert passed_user == _USER
    assert enabled_name in passed_connector_ids
    # Every passed id corresponds to an enabled connector — nothing disabled leaks.
    all_rows = await connectors_service.list_connectors(_WS, user_id=_USER)
    enabled_set = {r.name for r in all_rows if r.enabled}
    disabled_names = {r.name for r in all_rows if not r.enabled}
    assert set(passed_connector_ids) <= enabled_set
    assert not (set(passed_connector_ids) & disabled_names)


# ---------------------------------------------------------------------------
# Action gate — 403 without connector.execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_requires_connector_execute_action(monkeypatch, mongo_db):  # noqa: ARG001
    """A caller without connector.execute is denied 403 before any run fires."""
    from pocketpaw_ee.cloud.discovery import service as discovery_service

    fake_orch = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(discovery_service, "run_discovery_and_propose", fake_orch)

    app = _build_app(monkeypatch, allow=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/api/v1/cloud/discovery/run", json={})

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "connector.execute_denied"
    # The denial happens at the gate — the orchestrator must never have run.
    await asyncio.sleep(0)
    assert fake_orch.await_count == 0


# ---------------------------------------------------------------------------
# Workspace resolves from the active workspace, not a path param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_resolves_workspace_from_active_not_path(client: AsyncClient, monkeypatch):
    """The route has NO {workspace_id} path param — the workspace the
    orchestrator runs against comes from current_workspace_id (the fake user's
    active workspace), not from the URL."""
    from pocketpaw_ee.cloud.discovery import service as discovery_service

    fake_orch = AsyncMock(return_value=_fake_result())
    monkeypatch.setattr(discovery_service, "run_discovery_and_propose", fake_orch)

    # A bare /run path (no workspace segment) is the only valid shape.
    resp = await client.post("/api/v1/cloud/discovery/run", json={})
    assert resp.status_code == 202, resp.text
    await asyncio.sleep(0)

    assert fake_orch.await_count == 1
    assert fake_orch.await_args.args[0] == _WS

    # A would-be path-param variant does not exist — 404/405, never 202.
    bad = await client.post(f"/api/v1/cloud/discovery/{_WS}/run", json={})
    assert bad.status_code in (404, 405)
