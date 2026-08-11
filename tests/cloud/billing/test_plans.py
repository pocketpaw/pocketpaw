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
# Updated 2026-06-30 (feat/billing-quota-enforcement, chunk 1): added coverage for
#   the per-plan ``monthly_ceiling`` (the credit-quota cap later chunks enforce):
#   each tier carries the approved ceiling (free=1000 explicit trial, go/pro/pro_max
#   = allotment × 1.5, enterprise=None uncapped), and an unknown key built directly
#   FAILS CLOSED to the Free ceiling (never None/uncapped).
# Updated 2026-07-08 (feat/billing-smb-caps): added coverage for the three SMB
#   resource caps (``max_seats`` / ``max_pockets`` / ``max_connectors``): every tier
#   carries an int cap (Enterprise is None = uncapped), Free ``max_seats`` == the
#   ``Workspace.seats`` default (5) so no workspace regresses, and an unknown key
#   built directly FAILS CLOSED to the Free value on all three (never None/uncapped).
#   The numbers under test are the GENEROUS PLACEHOLDERS pending the captain's
#   pricing call — these tests lock the machinery (present on every tier, uncapped
#   Enterprise, fail-closed default), not the exact figures.
# Updated 2026-08-08 (feat/billing-rbac-member-caps): the CONSUMER member caps are
#   now LOCKED — Free ``max_seats`` = 0 (a Free workspace cannot invite ANY
#   members), Paw Go = 5, Paw Pro = 25 total workspace members (owner included),
#   Paw Pro Max + Enterprise = None (uncapped). The seat gate is plan-authoritative,
#   so Free = 0 is an actual block, not a no-op. Also added the daily LiveKit
#   CALL-TIME budget (``max_call_seconds_per_day``): Free = 0 (no calls), Go =
#   1_800 (30 min), Pro = 7_200 (2 hrs), Pro Max = 28_800 (8 hrs), Enterprise =
#   None — enforced by ``livekit.service.create_room``. These tests lock the
#   exact numbers.

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
# The per-plan monthly credit CEILING (the cap later chunks enforce). The rule is
# allotment × 1.5 for the paid usage tiers; Free is an explicit trial cap (its
# allotment is 0, so ×1.5 can't derive it); Enterprise is uncapped (None).
EXPECTED_CEILINGS: dict[str, int | None] = {
    "free": 1_000,
    "go": 2_250,
    "pro": 11_250,
    "pro_max": 45_000,
    "enterprise": None,
}
# The user-facing usage labels the UI shows INSTEAD of raw credits.
EXPECTED_USAGE_LABELS = {
    "free": "Limited",
    "go": "Everyday",
    "pro": "5× the usage",
    "pro_max": "20× the usage",
    "enterprise": "Custom",
}
# The three SMB resource caps (feat/billing-smb-caps). ``max_seats`` is the
# APPROVED CONSUMER member cap — Free = 0 (no invitations allowed), Paw Go = 5,
# Paw Pro = 25 total workspace members (owner included), Paw Pro Max +
# Enterprise = None (uncapped). Pockets + connectors remain their tier ceilings.
EXPECTED_MAX_SEATS: dict[str, int | None] = {
    "free": 0,
    "go": 5,
    "pro": 25,
    "pro_max": None,
    "enterprise": None,
}
EXPECTED_MAX_POCKETS: dict[str, int | None] = {
    "free": 200,
    "go": 1_000,
    "pro": 5_000,
    "pro_max": 20_000,
    "enterprise": None,
}
EXPECTED_MAX_CONNECTORS: dict[str, int | None] = {
    "free": 50,
    "go": 100,
    "pro": 250,
    "pro_max": 1_000,
    "enterprise": None,
}
# The daily LiveKit CALL-TIME budget per workspace, in SECONDS (2026-08-08).
# Free = 0 (no calls), Go = 1800 (30 min), Pro = 7200 (2 hrs), Pro Max = 28800
# (8 hrs), Enterprise = None (uncapped). Enforced by livekit.service at
# call-start time; an over-budget single call is force-ended at its deadline.
EXPECTED_MAX_CALL_SECONDS_PER_DAY: dict[str, int | None] = {
    "free": 0,
    "go": 1_800,
    "pro": 7_200,
    "pro_max": 28_800,
    "enterprise": None,
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


def test_list_plans_ceilings_are_the_exact_ladder():
    """Each tier carries the approved monthly_ceiling, incl. enterprise=None.

    free=1000 (explicit trial cap), go/pro/pro_max = allotment × 1.5, enterprise
    uncapped (None). This is the cap later chunks enforce against the wallet.
    """
    by_key = {p.key: p for p in plans.list_plans()}
    for key, expected in EXPECTED_CEILINGS.items():
        assert by_key[key].monthly_ceiling == expected, key


def test_paid_tier_ceilings_are_allotment_times_one_point_five():
    """The go/pro/pro_max ceilings are exactly 1.5× their allotment (the rule)."""
    by_key = {p.key: p for p in plans.list_plans()}
    for key in ("go", "pro", "pro_max"):
        assert by_key[key].monthly_ceiling == int(by_key[key].monthly_credit_allotment * 1.5), key


def test_enterprise_ceiling_is_uncapped():
    """Enterprise is the one uncapped tier — its ceiling is None, never a number."""
    assert plans.get_plan("enterprise").monthly_ceiling is None


def test_free_ceiling_is_the_explicit_trial_cap():
    """Free's ceiling is the explicit 1000 trial cap (its 0 allotment can't derive it)."""
    free = plans.get_plan("free")
    assert free.monthly_credit_allotment == 0
    assert free.monthly_ceiling == 1_000


def test_build_unknown_key_fails_closed_to_free_ceiling():
    """An unknown key built directly never yields None/uncapped — it caps at Free.

    Callers reach the catalog via get_plan/list_plans (known keys only), but the
    ceiling default is the fail-closed floor regardless: a stray key must NOT
    produce an uncapped (None) ceiling.
    """
    bogus = plans._build("definitely_not_a_tier")
    assert bogus.monthly_ceiling == EXPECTED_CEILINGS["free"]
    assert bogus.monthly_ceiling is not None


# ---------------------------------------------------------------------------
# SMB resource caps — max_seats / max_pockets / max_connectors.
# ---------------------------------------------------------------------------


def test_every_tier_carries_the_smb_caps():
    """Each tier exposes max_seats / max_pockets / max_connectors + the daily
    LiveKit call budget (the machinery). Locks that the caps are PRESENT on
    every tier with the approved values."""
    by_key = {p.key: p for p in plans.list_plans()}
    for key in EXPECTED_ORDER:
        assert by_key[key].max_seats == EXPECTED_MAX_SEATS[key], key
        assert by_key[key].max_pockets == EXPECTED_MAX_POCKETS[key], key
        assert by_key[key].max_connectors == EXPECTED_MAX_CONNECTORS[key], key
        assert by_key[key].max_call_seconds_per_day == EXPECTED_MAX_CALL_SECONDS_PER_DAY[key], key


def test_free_max_seats_is_zero_no_invites():
    """Free max_seats is 0 — a Free workspace cannot invite ANY members.

    The ABAC/RBAC member gate (``workspace.service._effective_seat_limit``) is
    plan-authoritative and blocks an invite once ``member_count >= max_seats``;
    a Free workspace's owner alone already fills a 0-seat cap, so every invite
    raises SeatLimitError. This is the CRITICAL consumer-pricing invariant.
    """
    assert plans.get_plan("free").max_seats is not None
    assert plans.get_plan("free").max_seats == 0


def test_paid_tier_seats_are_the_consumer_ladder():
    """The paid tiers carry the approved caps: Go=5, Pro=25, ProMax=Unlimited.

    Each count is TOTAL workspace members (owner included), so Paw Go allows the
    owner + 4 invited members, Paw Pro + 24; Paw Pro Max is uncapped (None).
    """
    assert plans.get_plan("go").max_seats == 5
    assert plans.get_plan("pro").max_seats == 25
    assert plans.get_plan("pro_max").max_seats is None


def test_enterprise_smb_caps_are_uncapped():
    """Enterprise is fully uncapped on every SMB cap (None, never a number)."""
    ent = plans.get_plan("enterprise")
    assert ent.max_seats is None
    assert ent.max_pockets is None
    assert ent.max_connectors is None


def test_pro_max_seats_are_uncapped_but_other_caps_concrete():
    """Paw Pro Max is uncapped on SEATS only; pockets + connectors stay capped."""
    pro_max = plans.get_plan("pro_max")
    assert pro_max.max_seats is None
    assert isinstance(pro_max.max_pockets, int) and pro_max.max_pockets > 0
    assert isinstance(pro_max.max_connectors, int) and pro_max.max_connectors > 0


def test_daily_call_budget_is_the_consumer_ladder():
    """The daily LiveKit call budget: Free=0 (no calls), Go=30min, Pro=2hrs,
    Pro Max=8hrs, Enterprise=None (uncapped)."""
    assert plans.get_plan("free").max_call_seconds_per_day == 0
    assert plans.get_plan("go").max_call_seconds_per_day == 30 * 60
    assert plans.get_plan("pro").max_call_seconds_per_day == 2 * 60 * 60
    assert plans.get_plan("pro_max").max_call_seconds_per_day == 8 * 60 * 60
    assert plans.get_plan("enterprise").max_call_seconds_per_day is None


def test_non_enterprise_smb_caps_are_non_negative_ints():
    """Every tier below Pro Max carries a concrete int cap on all three.

    ``max_seats`` may be 0 (Free — the "no member invites" cap is a valid
    ceiling); pockets + connectors are always positive on the capped tiers.
    """
    by_key = {p.key: p for p in plans.list_plans()}
    for key in ("free", "go", "pro"):
        assert isinstance(by_key[key].max_seats, int) and by_key[key].max_seats >= 0, key
        assert isinstance(by_key[key].max_pockets, int) and by_key[key].max_pockets > 0, key
        assert isinstance(by_key[key].max_connectors, int) and by_key[key].max_connectors > 0, key
        # The daily call budget is a concrete non-negative int on every capped
        # tier too (Free = 0 — "no calls" is a valid ceiling).
        assert (
            isinstance(by_key[key].max_call_seconds_per_day, int)
            and by_key[key].max_call_seconds_per_day >= 0
        ), key


def test_build_unknown_key_fails_closed_to_free_smb_caps():
    """An unknown key built directly caps at the Free values on all three — never None.

    Same fail-closed floor the ceiling uses: a stray/typo'd key must NOT resolve to
    an uncapped SMB cap.
    """
    bogus = plans._build("definitely_not_a_tier")
    assert bogus.max_seats == EXPECTED_MAX_SEATS["free"]
    assert bogus.max_pockets == EXPECTED_MAX_POCKETS["free"]
    assert bogus.max_connectors == EXPECTED_MAX_CONNECTORS["free"]
    # The daily call budget fails closed too: an unknown key gets Free's 0
    # (no calls), never None/uncapped.
    assert bogus.max_call_seconds_per_day == EXPECTED_MAX_CALL_SECONDS_PER_DAY["free"]
    assert bogus.max_call_seconds_per_day == 0
    assert bogus.max_seats is not None
    assert bogus.max_pockets is not None
    assert bogus.max_connectors is not None
    assert bogus.max_call_seconds_per_day is not None


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
