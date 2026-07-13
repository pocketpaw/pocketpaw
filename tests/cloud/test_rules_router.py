# test_rules_router.py — HTTP-layer tests for ee/cloud/rules/router.py.
# Created: 2026-07-09 (feat/instinct-guardrail-rules) — smokes the thin
# /api/v1/rules surface over the shipped rules.service: create persists an active
# InstinctRuleDoc, list is tenant-filtered (a workspace sees only its own active
# rules), archive flips a rule to archived (and it drops out of the list), the
# service's cross-tenant-scope guard surfaces as a 400, the enforcement GET/PUT
# round-trips, and the auth/permission seams (401 unauth, 403 without rules.manage).
#
# The router calls the REAL service against the mongomock-backed ``mongo_db``
# fixture (Beanie), so these are true persistence smokes, not Protocol fakes.
# Auth: ``current_active_user`` is overridden to a SimpleNamespace user and RBAC's
# ``check_workspace_action`` is patched on the consumer module (mirrors
# test_audit_router.py).

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.instinct_rule import InstinctRuleDoc
from pocketpaw_ee.cloud.rules.router import router as rules_router

pytestmark = pytest.mark.usefixtures("mongo_db")


def _fake_user(user_id: str = "u1", workspace_id: str | None = "w1") -> SimpleNamespace:
    """User stand-in shaped like ``ee.cloud.models.user.User`` — only the
    attributes the rules-router auth chain reads (``id``, ``active_workspace``,
    ``workspaces``)."""
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")] if workspace_id else [],
    )


def _build_app(
    workspace_id: str | None = "w1",
    user_id: str = "u1",
    *,
    skip_auth_override: bool = False,
    permission_denier: bool = False,
    monkeypatch=None,
) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(rules_router)
    app.dependency_overrides[require_license] = lambda: None

    if not skip_auth_override:
        user = _fake_user(user_id=user_id, workspace_id=workspace_id)

        async def _fake_user_dep():
            return user

        app.dependency_overrides[current_active_user] = _fake_user_dep

        if monkeypatch is not None:
            from pocketpaw_ee.cloud._core import deps as core_deps
            from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

            if permission_denier:

                def _deny(*_a, **_k):
                    raise GuardForbidden(
                        code="workspace.insufficient_role",
                        detail="no rules.manage",
                    )

                monkeypatch.setattr(core_deps, "check_workspace_action", _deny)
            else:
                monkeypatch.setattr(core_deps, "check_workspace_action", lambda *a, **k: None)

    return app


def _create_body(workspace_id: str = "w1", *, name: str = "big-write approval") -> dict:
    """A valid ``CreateRuleRequest`` JSON body — a governed rule that escalates
    writes over $500, scoped to ``workspace_id``."""
    return {
        "draft": {
            "name": name,
            "description": "writes over $500 need approval",
            "when": "value > 500",
            "action": "require_approval",
            "scope": {"workspace_id": workspace_id},
            "confidence": 0.9,
            "provenance": ["ui-authored"],
        },
        "owner_user_id": "u1",
    }


@pytest_asyncio.fixture
async def w1_client(monkeypatch) -> AsyncClient:
    app = _build_app(workspace_id="w1", monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(monkeypatch) -> AsyncClient:
    app = _build_app(workspace_id="w2", monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Create → persist
# ---------------------------------------------------------------------------


async def test_create_persists_active_rule(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/rules", json=_create_body("w1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["when"] == "value > 500"
    assert body["workspace_id"] == "w1"
    assert body["id"]

    # It persists as an active InstinctRuleDoc.
    doc = await InstinctRuleDoc.find_one(InstinctRuleDoc.id == PydanticObjectId(body["id"]))
    assert doc is not None
    assert doc.status == "active"
    assert doc.workspace == "w1"


async def test_list_returns_active_rules_for_workspace(w1_client: AsyncClient) -> None:
    await w1_client.post("/rules", json=_create_body("w1", name="rule-a"))
    r = await w1_client.get("/rules")
    assert r.status_code == 200, r.text
    names = {row["name"] for row in r.json()}
    assert names == {"rule-a"}


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_list_is_tenant_isolated(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """A rule created in w1 is invisible to w2's list, and vice-versa."""
    await w1_client.post("/rules", json=_create_body("w1", name="w1-secret"))
    await w2_client.post("/rules", json=_create_body("w2", name="w2-secret"))

    r1 = await w1_client.get("/rules")
    r2 = await w2_client.get("/rules")
    assert {row["name"] for row in r1.json()} == {"w1-secret"}
    assert {row["name"] for row in r2.json()} == {"w2-secret"}


async def test_create_rejects_cross_tenant_scope(w1_client: AsyncClient) -> None:
    """A w1 caller cannot persist a rule scoped to w2 — the service's tenancy
    assertion surfaces as a 400 CloudError, never a silent cross-tenant write."""
    r = await w1_client.post("/rules", json=_create_body("w2"))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "rule.workspace_mismatch"


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


async def test_archive_flips_rule_and_drops_from_list(w1_client: AsyncClient) -> None:
    created = (await w1_client.post("/rules", json=_create_body("w1"))).json()
    rule_id = created["id"]

    r = await w1_client.post(f"/rules/{rule_id}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"

    listed = (await w1_client.get("/rules")).json()
    assert all(row["id"] != rule_id for row in listed)


async def test_archive_cannot_reach_across_tenant(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    """w2 cannot archive a rule that lives in w1 — the tenant-scoped lookup
    misses and returns a 400 not-found CloudError."""
    created = (await w1_client.post("/rules", json=_create_body("w1"))).json()
    r = await w2_client.post(f"/rules/{created['id']}/archive")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "rule.not_found"


# ---------------------------------------------------------------------------
# Enforcement toggle round-trip
# ---------------------------------------------------------------------------


async def test_enforcement_get_defaults_and_put_sets_override(w1_client: AsyncClient) -> None:
    # Fresh workspace → no override, effective == global default (False).
    g = await w1_client.get("/rules/enforcement")
    assert g.status_code == 200, g.text
    assert g.json() == {
        "workspace_id": "w1",
        "enforce_discovered_rules": False,
        "override": None,
        "global_default": False,
    }

    # PUT enabled=true → override wins, effective True even though global is False.
    p = await w1_client.put("/rules/enforcement", json={"enabled": True})
    assert p.status_code == 200, p.text
    assert p.json()["override"] is True
    assert p.json()["enforce_discovered_rules"] is True

    # Clear the override (null) → back to inheriting the global default.
    c = await w1_client.put("/rules/enforcement", json={"enabled": None})
    assert c.status_code == 200, c.text
    assert c.json()["override"] is None
    assert c.json()["enforce_discovered_rules"] is False


# ---------------------------------------------------------------------------
# Auth / permission seams
# ---------------------------------------------------------------------------


async def test_missing_auth_returns_401() -> None:
    app = _build_app(workspace_id="w1", skip_auth_override=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/rules")
        assert r.status_code == 401


async def test_missing_rules_manage_permission_returns_403(monkeypatch) -> None:
    app = _build_app(workspace_id="w1", permission_denier=True, monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/rules")
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "workspace.insufficient_role"


# Silence ruff unused-import nudge on fixture-composition helpers.
_unused: tuple[Any, ...] = ()
