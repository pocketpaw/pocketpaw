# Tests for ee/cloud require_plan_feature FastAPI dependency.
# Created: 2026-05-07
# Covers plan-tier gating for sites (go+), the Fabric ontology fabric flag
# (enterprise-only), and instinct (EVERY tier — see the 2026-09-01 note below).
# Patches workspace_service.get_workspace_plan so no DB is needed.
# Updated 2026-06-25 (feat/consumer-plan-ladder): rekeyed the plan strings from
#   {team, business, enterprise} to the consumer ladder {free, go, pro, pro_max,
#   enterprise}.
# Updated 2026-06-25 (decouple-sites-from-fabric): the overloaded ``fabric`` flag
#   was split. Sites/Leads now gate on a NEW ``sites`` flag (go+) — go is ALLOWED,
#   free is DENIED. The ``fabric`` flag is now the Fabric ONTOLOGY gate and is
#   ENTERPRISE-ONLY — pro/pro_max are DENIED, enterprise is ALLOWED.
# Updated 2026-09-01 (fix/instinct-is-a-gate-not-a-tier): the two instinct DENIAL
#   tests became ALLOW tests across every tier. They were pinning a gate that was
#   half-open — ~20 modules write Instinct proposals without checking the flag,
#   and only the router that approves them carried it, so free/go/pro/pro_max
#   accumulated proposals nobody could act on. A workspace OWNER could not publish
#   a site for this reason: the builder self-approves through the Instinct API and
#   got ``plan.feature_denied`` no matter the role. Approving remains ADMIN.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_plan_feature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(feature: str, *, fixed_workspace_id: str = "ws-test") -> FastAPI:
    """Build a minimal FastAPI app with require_plan_feature guarding one route.

    current_workspace_id is overridden to return a fixed ID so no JWT or
    User model is involved. The workspace plan is controlled per-test by
    patching workspace_service.get_workspace_plan.
    """
    from pocketpaw_ee.cloud._core.http import add_error_handler

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
        import pocketpaw_ee.cloud.workspace.service as ws_svc

        monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value=plan))

    return _patch


# ---------------------------------------------------------------------------
# sites — go+ (Sites + Leads; decoupled from the fabric ontology)
# ---------------------------------------------------------------------------


class TestSitesFeatureGate:
    """require_plan_feature("sites") allows go/pro/pro_max/enterprise, blocks free.

    Sites (and Leads) unlock at Paw Go on the consumer ladder. This is the gate
    Sites was moved ONTO when the overloaded ``fabric`` flag was split.
    """

    def test_member_on_go_plan_can_access_sites(self, patch_plan):
        """Paw Go gets a site, so the go plan passes the sites gate."""
        patch_plan("go")
        client = TestClient(_build_app("sites"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_member_on_free_plan_is_denied_sites(self, patch_plan):
        """The free floor has no Sites — 403 with plan.feature_denied."""
        patch_plan("free")
        client = TestClient(_build_app("sites"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "plan.feature_denied"

    def test_pro_and_enterprise_can_access_sites(self, patch_plan):
        """Every paid tier above go also carries sites (it nests upward)."""
        for plan in ("pro", "pro_max", "enterprise"):
            patch_plan(plan)
            client = TestClient(_build_app("sites"), raise_server_exceptions=False)
            assert client.get("/guarded").status_code == 200, plan

    def test_error_message_names_go_as_minimum(self, patch_plan):
        """The free-plan denial names "go" as the minimum tier that unlocks Sites."""
        patch_plan("free")
        client = TestClient(_build_app("sites"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert "go" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# fabric — the Fabric ONTOLOGY, enterprise-only (decoupled from Sites)
# ---------------------------------------------------------------------------


class TestFabricOntologyFeatureGate:
    """require_plan_feature("fabric") is the Fabric ONTOLOGY gate: enterprise-only.

    After decoupling, ``fabric`` no longer gates Sites/Leads — it gates only the
    enterprise ontology API, so pro/pro_max are DENIED and only enterprise passes.
    """

    def test_enterprise_can_access_the_fabric_ontology(self, patch_plan):
        patch_plan("enterprise")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_pro_is_denied_the_fabric_ontology(self, patch_plan):
        """The decouple fix: pro must NOT inherit the enterprise-only ontology."""
        patch_plan("pro")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "plan.feature_denied"

    def test_pro_max_is_denied_the_fabric_ontology(self, patch_plan):
        """Pro Max also must NOT carry the ontology (the ladder is intentionally
        NOT a superset on the fabric flag)."""
        patch_plan("pro_max")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "plan.feature_denied"

    def test_go_is_denied_the_fabric_ontology(self, patch_plan):
        patch_plan("go")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# instinct — enterprise-only
# ---------------------------------------------------------------------------


class TestInstinctFeatureGate:
    """require_plan_feature("instinct") allows EVERY tier — it is a gate, not a tier.

    Instinct is the approval layer agents propose through, not a capability a
    workspace buys. Pricing it above the tiers that run agents left those tiers
    creating proposals nobody could read, approve, or reject: ~20 modules WRITE
    Instinct actions and almost none check this flag, while the single router
    that reads and approves them carried the gate. The reported symptom was a
    workspace OWNER unable to publish a site — the builder's Publish button
    self-approves through ``instinct/actions/pending`` + ``/approve``, and both
    403'd with ``plan.feature_denied`` regardless of role.

    Approving is still ADMIN (``guards/actions.py``); only the BILLING gate moved.
    """

    @pytest.mark.parametrize("plan", ["free", "go", "pro", "pro_max", "enterprise"])
    def test_every_tier_can_reach_instinct(self, patch_plan, plan):
        """The approval gate exists on every tier, including the free floor.

        MUTATION: drop ``"instinct"`` from any tier in ``PLAN_FEATURES`` and that
        tier's parametrization fails with 403 ``plan.feature_denied``.
        """
        patch_plan(plan)
        client = TestClient(_build_app("instinct"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200, f"{plan} must reach the approval gate"

    def test_the_free_floor_that_runs_agents_also_gets_the_gate(self, patch_plan):
        """The floor carries ``sessions``, so agents ACT there.

        This is the coherence rule the tier list has to keep: any tier that can
        run an agent can produce a proposal, and a proposal nobody can approve is
        worse than no gate at all. Pinned separately from the parametrized case
        because it is the reasoning, not just the value.
        """
        from pocketpaw_ee.guards.abac import PLAN_FEATURES

        for plan, features in PLAN_FEATURES.items():
            if "sessions" in features:
                assert "instinct" in features, (
                    f"{plan!r} runs agent sessions but cannot reach the approval "
                    "gate — its proposals would strand unapprovable"
                )


# ---------------------------------------------------------------------------
# Fallback / edge cases
# ---------------------------------------------------------------------------


class TestPlanFeatureGateEdgeCases:
    """Edge cases: unknown plan has no features (deny restricted), and the lowest
    consumer tier still passes a base feature."""

    def test_unknown_plan_denies_restricted_feature(self, patch_plan):
        """An unrecognised plan string has no features; restricted feature denied."""
        patch_plan("legacy_gold")  # not a real plan tier
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403

    def test_base_feature_passes_on_go_plan(self, patch_plan):
        """Features available on the go plan (e.g. pockets) are always accessible."""
        patch_plan("go")
        client = TestClient(_build_app("pockets"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 200

    def test_workspace_on_go_denies_enterprise_only_feature(self, monkeypatch):
        """A go-plan workspace is denied an enterprise-only feature (the fabric
        ontology) rather than 500."""
        import pocketpaw_ee.cloud.workspace.service as ws_svc

        monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="go"))
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "plan.feature_denied"

    def test_error_message_names_needed_plan(self, patch_plan):
        """The 403 body message names the minimum tier — enterprise for the fabric
        ontology (now enterprise-only after the decouple)."""
        patch_plan("go")
        client = TestClient(_build_app("fabric"), raise_server_exceptions=False)
        resp = client.get("/guarded")
        assert resp.status_code == 403
        # The fabric ontology is enterprise-only, so the needed plan is enterprise.
        assert "enterprise" in resp.json()["error"]["message"]
