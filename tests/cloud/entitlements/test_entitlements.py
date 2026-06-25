# tests/cloud/entitlements/test_entitlements.py — proves the BC-6 Entitlement-
# primitive contract: ``resolve_entitlements(workspace_id)`` maps a workspace to
# its plan + features + monthly credit allotment, derived from the EXISTING
# ``Workspace.plan`` field via ``get_workspace_plan`` and the billing plan
# catalog. Key invariants:
#   (a) a ``plan="business"`` workspace resolves the business feature set + its
#       allotment (features straight from PLAN_FEATURES);
#   (b) a workspace with no / unknown plan, or one that doesn't exist, falls back
#       to the ``free`` base tier — no crash, no paid-tier leak;
#   (c) the resolver reads the plan of the asked-for workspace ONLY (a second
#       tenant's plan never leaks across).
#
# Two layers: unit tests patch ``get_workspace_plan`` (no DB) to drive every plan
# string; one DB-backed test inserts REAL ``Workspace`` docs (mongo_db fixture)
# to prove the end-to-end read AND tenant isolation. The HTTP route is exercised
# with a TestClient + overridden deps (license waived, workspace pinned).
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new test module.
# Updated 2026-06-25 (feat/consumer-plan-ladder): rekeyed the plan strings under
#   test from {free, team, business, enterprise} to the consumer ladder
#   {free, go, pro, pro_max, enterprise}. The business-tier assertions now target
#   ``pro`` (its consumer-ladder successor: the tier that carries fabric +
#   automations + knowledge_base). Fallback-to-free invariants are unchanged.

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.entitlements import service as entitlements
from pocketpaw_ee.cloud.entitlements.router import router as entitlements_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_workspace_id
from pocketpaw_ee.guards.abac import PLAN_FEATURES

WS = "ws_entitlements_test"


@pytest.fixture
def patch_plan(monkeypatch: pytest.MonkeyPatch):
    """Patch get_workspace_plan to return a fixed plan string (or None)."""

    def _patch(plan: str | None) -> None:
        import pocketpaw_ee.cloud.workspace.service as ws_svc

        monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value=plan))

    return _patch


# ---------------------------------------------------------------------------
# Criterion (a) — a business workspace resolves business features + allotment.
# ---------------------------------------------------------------------------


async def test_pro_plan_resolves_pro_features_and_allotment(patch_plan):
    patch_plan("pro")
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.workspace_id == WS
    assert ent.plan == "pro"
    assert set(ent.features) == PLAN_FEATURES["pro"]
    assert ent.monthly_credit_allotment > 0
    # The catalog's pro allotment is exactly what resolves.
    from pocketpaw_ee.cloud.billing import plans

    assert ent.monthly_credit_allotment == plans.get_plan("pro").monthly_credit_allotment


@pytest.mark.parametrize("plan", ["free", "go", "pro", "pro_max", "enterprise"])
async def test_every_known_plan_resolves_its_own_features(patch_plan, plan):
    patch_plan(plan)
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == plan
    assert set(ent.features) == PLAN_FEATURES[plan]


# ---------------------------------------------------------------------------
# Criterion (b) — no/unknown plan, or missing workspace, falls back to free.
# ---------------------------------------------------------------------------


async def test_unknown_plan_falls_back_to_free(patch_plan):
    """A stale / typo'd plan string is NOT trusted — resolve to the free floor."""
    patch_plan("legacy_gold_tier")
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert set(ent.features) == PLAN_FEATURES["free"]
    assert ent.monthly_credit_allotment == 0


async def test_missing_workspace_falls_back_to_free(patch_plan):
    """get_workspace_plan returns None (missing/deleted/bad id) -> free, no crash."""
    patch_plan(None)
    ent = await entitlements.resolve_entitlements(WS)
    assert ent.plan == "free"
    assert ent.monthly_credit_allotment == 0
    # Crucially, free must NOT carry any paid-tier feature.
    assert "fabric" not in ent.features
    assert "instinct" not in ent.features


async def test_empty_workspace_id_is_rejected_at_entry():
    """Rule 6 — an empty workspace_id raises ValidationError, not a silent free."""
    with pytest.raises(ValidationError):
        await entitlements.resolve_entitlements("")


# ---------------------------------------------------------------------------
# DB-backed — real Workspace docs prove end-to-end read + tenant isolation.
# ---------------------------------------------------------------------------


async def test_resolve_reads_real_workspace_plan_and_isolates_tenants(mongo_db):
    """Two workspaces with different plans each resolve their OWN plan only."""
    from pocketpaw_ee.cloud.models.workspace import Workspace

    biz_ws = Workspace(name="Acme", slug="acme-pro", owner="u-owner", plan="pro")
    await biz_ws.insert()
    free_ws = Workspace(name="Beta", slug="beta-free", owner="u-owner2", plan="free")
    await free_ws.insert()

    biz_ent = await entitlements.resolve_entitlements(str(biz_ws.id))
    free_ent = await entitlements.resolve_entitlements(str(free_ws.id))

    # Each resolves its OWN plan — no cross-tenant leak.
    assert biz_ent.plan == "pro"
    assert set(biz_ent.features) == PLAN_FEATURES["pro"]
    assert free_ent.plan == "free"
    assert set(free_ent.features) == PLAN_FEATURES["free"]
    # The free tenant did NOT pick up the pro tenant's paid features.
    assert "fabric" not in free_ent.features


async def test_resolve_for_nonexistent_id_is_free(mongo_db):
    """A well-formed but nonexistent workspace id resolves to free (no crash)."""
    from bson import ObjectId

    ent = await entitlements.resolve_entitlements(str(ObjectId()))
    assert ent.plan == "free"
    assert ent.monthly_credit_allotment == 0


# ---------------------------------------------------------------------------
# GET /entitlements — the HTTP read surface.
# ---------------------------------------------------------------------------


@pytest.fixture
def entitlements_client() -> TestClient:
    """TestClient over the entitlements router; license waived, workspace pinned."""
    app = FastAPI()
    app.include_router(entitlements_router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app, raise_server_exceptions=False)


def test_get_entitlements_returns_resolved_entitlements(entitlements_client, patch_plan):
    patch_plan("pro")
    resp = entitlements_client.get("/entitlements")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspace_id"] == WS
    assert body["plan"] == "pro"
    assert set(body["features"]) == PLAN_FEATURES["pro"]
    assert body["features"] == sorted(body["features"])  # deterministic JSON
    assert body["monthly_credit_allotment"] > 0


def test_get_entitlements_unknown_plan_is_free(entitlements_client, patch_plan):
    patch_plan(None)
    resp = entitlements_client.get("/entitlements")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "free"
    assert body["monthly_credit_allotment"] == 0
