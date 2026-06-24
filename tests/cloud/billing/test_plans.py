# tests/cloud/billing/test_plans.py — proves the BC-6 Plan-catalog contract:
# the declarative plan catalog (``billing.plans``) and the ``GET /billing/plans``
# read surface expose every tier with its monthly credit allotment + features +
# Dodo product id, the features come STRAIGHT from ``PLAN_FEATURES`` (one source
# of truth, no duplication), and ``get_plan`` resolves a known key / rejects an
# unknown one.
#
# Pure-function + HTTP-layer tests — no DB. ``list_plans`` / ``get_plan`` are
# synchronous catalog builders over ``PLAN_FEATURES``; the route is exercised
# with a FastAPI ``TestClient`` and the ``require_license`` dep overridden (same
# pattern as test_plan_feature_gate.py — no JWT, no Mongo).
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new test module.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.billing import plans
from pocketpaw_ee.cloud.billing.router import router as billing_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.guards.abac import PLAN_FEATURES

EXPECTED_TIERS = {"free", "team", "business", "enterprise"}


# ---------------------------------------------------------------------------
# Catalog — list_plans / get_plan are the source-of-truth-respecting view.
# ---------------------------------------------------------------------------


def test_list_plans_covers_every_plan_features_tier():
    """Every tier in PLAN_FEATURES shows up in the catalog (no tier dropped)."""
    keys = {p.key for p in plans.list_plans()}
    assert keys == set(PLAN_FEATURES.keys())
    assert EXPECTED_TIERS <= keys


def test_list_plans_features_match_plan_features_exactly():
    """Catalog features are SOURCED FROM PLAN_FEATURES — never duplicated/drifted."""
    for p in plans.list_plans():
        assert set(p.features) == PLAN_FEATURES[p.key], p.key


def test_list_plans_allotment_ladder_is_monotonic_and_free_is_zero():
    """free grants nothing; each paid tier is a strict step up the credit ladder."""
    by_key = {p.key: p for p in plans.list_plans()}
    assert by_key["free"].monthly_credit_allotment == 0
    assert (
        by_key["free"].monthly_credit_allotment
        < by_key["team"].monthly_credit_allotment
        < by_key["business"].monthly_credit_allotment
        < by_key["enterprise"].monthly_credit_allotment
    )


def test_list_plans_is_cheapest_first():
    """The catalog is ordered cheapest tier first (the price ladder)."""
    listed = [p.key for p in plans.list_plans()]
    assert listed[:4] == ["free", "team", "business", "enterprise"]


def test_dodo_product_id_is_none_until_bc7():
    """No Dodo product id is wired yet — every tier reads back None in v1."""
    assert all(p.dodo_product_id is None for p in plans.list_plans())


def test_get_plan_resolves_a_known_key():
    """get_plan returns the right tier for a known key, with matching features."""
    biz = plans.get_plan("business")
    assert biz is not None
    assert biz.key == "business"
    assert set(biz.features) == PLAN_FEATURES["business"]
    assert biz.monthly_credit_allotment > 0


@pytest.mark.parametrize("bad", ["nope", "", None, "FREE", "Team"])
def test_get_plan_rejects_unknown_or_missing_key(bad):
    """An unknown / empty / None / wrong-case key resolves to None (not a guess)."""
    assert plans.get_plan(bad) is None


def test_base_plan_key_is_a_real_catalog_entry():
    """The declared base/floor tier exists in the catalog (resolver relies on it)."""
    base = plans.get_plan(plans.BASE_PLAN_KEY)
    assert base is not None
    assert base.key == "free"
    assert base.monthly_credit_allotment == 0


# ---------------------------------------------------------------------------
# GET /billing/plans — the HTTP read surface.
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_client() -> TestClient:
    """A TestClient over an app mounting the billing router, license waived."""
    app = FastAPI()
    app.include_router(billing_router)
    app.dependency_overrides[require_license] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_get_billing_plans_returns_the_catalog(plans_client):
    """GET /billing/plans lists every tier with allotment + sorted features."""
    resp = plans_client.get("/billing/plans")
    assert resp.status_code == 200
    body = resp.json()
    rows = {row["key"]: row for row in body["plans"]}
    assert set(rows.keys()) == set(PLAN_FEATURES.keys())

    biz = rows["business"]
    assert biz["monthly_credit_allotment"] == plans.get_plan("business").monthly_credit_allotment
    # Features arrive as a sorted JSON array but carry the same SET as PLAN_FEATURES.
    assert set(biz["features"]) == PLAN_FEATURES["business"]
    assert biz["features"] == sorted(biz["features"])
    assert biz["dodo_product_id"] is None
