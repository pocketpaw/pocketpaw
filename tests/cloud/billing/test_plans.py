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
# Updated 2026-06-25 (feat/consumer-plan-ladder): rekeyed the expected tiers from
#   {free, team, business, enterprise} to the consumer ladder
#   {free, go, pro, pro_max, enterprise}; updated the allotment ladder
#   (0/1500/7500/30000/1_000_000); added coverage for the new user-facing display +
#   price fields (display_name, usage_label, usage_detail, INR/USD prices) the
#   billing UI renders instead of raw credits.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.billing import plans
from pocketpaw_ee.cloud.billing.router import router as billing_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.guards.abac import PLAN_FEATURES

EXPECTED_TIERS = {"free", "go", "pro", "pro_max", "enterprise"}
EXPECTED_ORDER = ["free", "go", "pro", "pro_max", "enterprise"]
EXPECTED_ALLOTMENTS = {
    "free": 0,
    "go": 1_500,
    "pro": 7_500,
    "pro_max": 30_000,
    "enterprise": 1_000_000,
}
# The user-facing usage labels the UI shows INSTEAD of raw credits.
EXPECTED_USAGE_LABELS = {
    "free": "Limited",
    "go": "Everyday",
    "pro": "5× the usage",
    "pro_max": "20× the usage",
    "enterprise": "Custom",
}


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
        < by_key["go"].monthly_credit_allotment
        < by_key["pro"].monthly_credit_allotment
        < by_key["pro_max"].monthly_credit_allotment
        < by_key["enterprise"].monthly_credit_allotment
    )


def test_list_plans_allotments_are_the_exact_ladder():
    """The allotment for each tier is the approved 0/1500/7500/30000/1_000_000."""
    by_key = {p.key: p for p in plans.list_plans()}
    for key, expected in EXPECTED_ALLOTMENTS.items():
        assert by_key[key].monthly_credit_allotment == expected, key


def test_list_plans_is_cheapest_first():
    """The catalog is ordered cheapest tier first (the price ladder)."""
    listed = [p.key for p in plans.list_plans()]
    assert listed == EXPECTED_ORDER


def test_list_plans_carries_display_and_price_fields():
    """Every tier carries the user-facing display + price metadata the UI shows.

    The headline is ``usage_label`` (shown instead of raw credits); ``free`` is
    all-zero; ``enterprise`` prices are None (\"talk to us\")."""
    by_key = {p.key: p for p in plans.list_plans()}

    # The usage labels are the headline wording the UI renders.
    for key, label in EXPECTED_USAGE_LABELS.items():
        assert by_key[key].usage_label == label, key

    assert by_key["free"].display_name == "Free"
    assert by_key["go"].display_name == "Paw Go"
    assert by_key["pro"].display_name == "Paw Pro"
    assert by_key["pro_max"].display_name == "Paw Pro Max"
    assert by_key["enterprise"].display_name == "Enterprise"

    # usage_detail is a short, non-empty supporting line on every tier.
    for p in plans.list_plans():
        assert p.usage_detail

    # Prices: free is 0 across the board; the paid tiers carry the approved
    # INR + USD monthly/annual integers; enterprise is None (talk to us).
    free = by_key["free"]
    assert (free.price_inr_monthly, free.price_inr_annual) == (0, 0)
    assert (free.price_usd_monthly, free.price_usd_annual) == (0, 0)

    go = by_key["go"]
    assert (go.price_inr_monthly, go.price_inr_annual) == (399, 3990)
    assert (go.price_usd_monthly, go.price_usd_annual) == (9, 90)

    pro = by_key["pro"]
    assert (pro.price_inr_monthly, pro.price_inr_annual) == (1499, 14990)
    assert (pro.price_usd_monthly, pro.price_usd_annual) == (19, 190)

    pro_max = by_key["pro_max"]
    assert (pro_max.price_inr_monthly, pro_max.price_inr_annual) == (4999, 49990)
    assert (pro_max.price_usd_monthly, pro_max.price_usd_annual) == (49, 490)

    ent = by_key["enterprise"]
    assert ent.price_inr_monthly is None and ent.price_inr_annual is None
    assert ent.price_usd_monthly is None and ent.price_usd_annual is None


def test_dodo_product_id_is_none_until_bc7():
    """No Dodo product id is wired yet — every tier reads back None in v1."""
    assert all(p.dodo_product_id is None for p in plans.list_plans())


def test_get_plan_resolves_a_known_key():
    """get_plan returns the right tier for a known key, with matching features."""
    pro = plans.get_plan("pro")
    assert pro is not None
    assert pro.key == "pro"
    assert set(pro.features) == PLAN_FEATURES["pro"]
    assert pro.monthly_credit_allotment > 0


@pytest.mark.parametrize("bad", ["nope", "", None, "FREE", "Go", "team", "business"])
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

    pro = rows["pro"]
    assert pro["monthly_credit_allotment"] == plans.get_plan("pro").monthly_credit_allotment
    # Features arrive as a sorted JSON array but carry the same SET as PLAN_FEATURES.
    assert set(pro["features"]) == PLAN_FEATURES["pro"]
    assert pro["features"] == sorted(pro["features"])
    assert pro["dodo_product_id"] is None


def test_get_billing_plans_exposes_the_consumer_ladder_with_labels_and_prices(plans_client):
    """The wire catalog is the five tiers in order, each with the UI's usage_label
    + display_name + prices populated and the approved credit allotments."""
    resp = plans_client.get("/billing/plans")
    assert resp.status_code == 200
    body = resp.json()

    # Five tiers, cheapest first.
    listed = [row["key"] for row in body["plans"]]
    assert listed == EXPECTED_ORDER

    rows = {row["key"]: row for row in body["plans"]}
    for key in EXPECTED_ORDER:
        row = rows[key]
        # The headline wording the UI shows instead of credits.
        assert row["usage_label"] == EXPECTED_USAGE_LABELS[key], key
        assert row["display_name"], key
        assert row["usage_detail"], key
        # Back-office allotment still on the wire (not the headline).
        assert row["monthly_credit_allotment"] == EXPECTED_ALLOTMENTS[key], key

    # Prices on the wire: go is the entry paid tier, enterprise is null.
    assert rows["go"]["price_inr_monthly"] == 399
    assert rows["go"]["price_usd_monthly"] == 9
    assert rows["pro"]["price_inr_annual"] == 14990
    assert rows["pro_max"]["price_usd_annual"] == 490
    assert rows["enterprise"]["price_inr_monthly"] is None
    assert rows["enterprise"]["price_usd_annual"] is None
