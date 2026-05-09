# Tests for ee/cloud require_plan_feature FastAPI dependency.
# Created: 2026-05-07
# Covers plan-tier gating for fabric (business+) and instinct (enterprise-only).
# Patches workspace_service.get_workspace_plan so no DB is needed.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.deps import current_workspace_id, require_plan_feature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(feature: str, *, fixed_workspace_id: str = "ws-test") -> FastAPI:
    """Build a minimal FastAPI app with require_plan_feature guarding one route.

    current_workspace_id is overridden to return a fixed ID so no JWT or
    User model is involved. The workspace plan is controlled per-test by
    patching workspace_service.get_workspace_plan.
    """
    from ee.cloud._core.http import add_error_handler

    app = FastAPI()
    add_error_handler(app)

    app.dependency_overrides[current_workspace_id] = lambda: fixed_workspace_id

    @app.get(
        "/guarded",
        dependencies=[Depends(require_plan_feature(feature))],
    )
    async def guarded_endpoint() -> dict[str, Any]:
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_plan(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that patches get_workspace_plan to return a fixed plan."""

    def _patch(plan: str) -> None:
        import ee.cloud.workspace.service as ws_svc

        monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value=plan))

    return _patch


# ---------------------------------------------------------------------------
# fabric — business+
# ---------------------------------------------------------------------------


class TestFabricFeatureGate:
    """require_plan_feature("fabric") allows business/enterprise, blocks team."""

    def test_member_on_business_plan_can_access_fabric(self, patch_plan):
        """A workspace on the business plan should pass the fabric gate."""
        patch_plan("business")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_member_on_team_plan_is_denied_fabric(self, patch_plan):
        """A workspace on the team plan must get 403 with plan.feature_denied."""
        patch_plan("team")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "plan.feature_denied"

    def test_enterprise_plan_can_access_fabric(self, patch_plan):
        """Enterprise plan includes all business features, so fabric is allowed."""
        patch_plan("enterprise")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# instinct — enterprise-only
# ---------------------------------------------------------------------------


class TestInstinctFeatureGate:
    """require_plan_feature("instinct") allows enterprise, blocks team and business."""

    def test_member_on_enterprise_plan_can_access_instinct(self, patch_plan):
        """Enterprise plan includes instinct."""
        patch_plan("enterprise")
        client = TestClient(_build_app("instinct"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200

    def test_admin_on_business_plan_is_denied_instinct(self, patch_plan):
        """Business plan does not include instinct; must return 403."""
        patch_plan("business")
        client = TestClient(_build_app("instinct"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "plan.feature_denied"

    def test_team_plan_is_denied_instinct(self, patch_plan):
        """Team plan does not include instinct; must return 403."""
        patch_plan("team")
        client = TestClient(_build_app("instinct"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "plan.feature_denied"


# ---------------------------------------------------------------------------
# Fallback / edge cases
# ---------------------------------------------------------------------------


class TestPlanFeatureGateEdgeCases:
    """Edge cases: unknown plan falls back to team behaviour (fail open on 500,
    deny on feature)."""

    def test_unknown_plan_denies_restricted_feature(self, patch_plan):
        """An unrecognised plan string has no features; restricted feature denied."""
        patch_plan("free")  # not a real plan tier
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403

    def test_team_feature_passes_on_team_plan(self, patch_plan):
        """Features available on team plan (e.g. pockets) are always accessible."""
        patch_plan("team")
        client = TestClient(_build_app("pockets"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200

    def test_workspace_not_found_denies_restricted_feature(self, monkeypatch):
        """When get_workspace_plan returns the 'team' fallback (workspace not found),
        a business+ feature is still denied rather than raising a 500."""
        import ee.cloud.workspace.service as ws_svc

        # Simulate the fallback: workspace missing, returns "team"
        monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="team"))
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "plan.feature_denied"

    def test_error_message_names_needed_plan(self, patch_plan):
        """The 403 body message should name the minimum required plan."""
        patch_plan("team")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        # Error message should mention "business" as the needed plan for fabric
        assert "business" in resp.json()["error"]["message"]
