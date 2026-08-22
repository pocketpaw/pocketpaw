# tests/cloud/billing/test_site_pricing_ladder.py — the per-site price ladder, as
# decided rather than as originally guessed.
#
# The old catalog was $0 / $120 / $480 ANNUAL, and the annual figures had no cost
# basis behind them (see docs/design/drafts/2026-08-21-paw-sites-pricing-revision.md:
# the real Cloudflare floor is $0.10 per custom hostname per month, and every
# comparable product on the market prices monthly in single digits). Annual-only
# billing was also a conversion problem in its own right.
#
# Decided 2026-08-22: $0 / $5 / $19, MONTHLY, per site.
#
# Two things beyond the numbers change with it, and both are easy to miss:
#
#   * The interval. A renewal webhook that stamps +365 days on a monthly plan puts
#     the next renewal a year out on every single renewal, and the site keeps its
#     paid capabilities for that whole year whatever happens to the card.
#   * The concierge. It used to be sold by ANY tier above the floor, derived rather
#     than mapped, because the tier that was meant to sell it did not exist yet.
#     Under this ladder only the TOP tier does — that is the entire difference
#     between the $5 and $19 rungs, so deriving it now hands the $5 tier a $19
#     feature.
#
# The tier KEYS deliberately do not change here. Renaming them (basic/pro/business
# to free/site/staff) rewrites values stored in Site.plan_tier, and an orphaned key
# resolves to None and drops a site to the floor. That is its own change, gated on
# the census confirming what production actually holds.
#
# Created 2026-08-22 (feat/site-pricing-monthly): new test module.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.billing.site_plans import SitePlanTier


def _by_key() -> dict[str, SitePlanTier]:
    return {t.key: t for t in site_plans.list_site_plans()}


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "price"),
    [("basic", 0), ("pro", 5), ("business", 19)],
)
def test_the_ladder_is_zero_five_nineteen(key, price):
    tier = site_plans.get_site_plan(key)
    assert tier is not None, f"{key} left the catalog"
    assert tier.monthly_price_usd == price


def test_the_price_is_monthly_and_nothing_still_calls_it_annual():
    """The rename is the point, not cosmetics. A field named ``annual_price_usd``
    holding 5 would read as $5/year to every future caller, and $5/year is below
    the Cloudflare floor for a single custom hostname."""
    tier = site_plans.get_site_plan("pro")

    assert hasattr(tier, "monthly_price_usd")
    assert not hasattr(tier, "annual_price_usd")


def test_the_floor_is_still_free():
    assert site_plans.get_site_plan(site_plans.BASE_SITE_PLAN_KEY).monthly_price_usd == 0


def test_the_ladder_only_goes_up():
    """A cheaper tier that sells more is a pricing bug, and the catalog is ordered
    cheapest-first for the buyer-facing cards."""
    prices = [t.monthly_price_usd for t in site_plans.list_site_plans()]

    assert prices == sorted(prices)
    assert len(set(prices)) == len(prices), "two tiers at the same price"


# ---------------------------------------------------------------------------
# The concierge is the difference between the two paid rungs
# ---------------------------------------------------------------------------


def test_only_the_top_tier_sells_the_concierge():
    """It used to be derived as "any tier above the floor", which was correct only
    while the tier meant to sell it did not exist. Deriving it now would hand the
    $5 rung the feature the $19 rung is for."""
    by_key = _by_key()

    assert by_key["basic"].sells_concierge is False
    assert by_key["pro"].sells_concierge is False, (
        "the $5 tier is selling the concierge, which is what the $19 tier is for"
    )
    assert by_key["business"].sells_concierge is True


def test_the_paid_rungs_are_actually_different_products():
    """If the two paid tiers sold exactly the same things, the ladder would be a
    price increase with no reason attached to it."""
    by_key = _by_key()
    mid, top = by_key["pro"], by_key["business"]

    assert (mid.sells_concierge, mid.badge_removal) != (top.sells_concierge, top.badge_removal) or (
        mid.cloudflare_features != top.cloudflare_features
    )


def test_both_paid_rungs_still_drop_the_badge_and_take_a_domain():
    """The $5 rung's whole pitch. Losing either while repricing would be a silent
    downgrade for anyone who buys it."""
    by_key = _by_key()

    for key in ("pro", "business"):
        assert by_key[key].badge_removal is True
        assert by_key[key].max_domained_sites is None, f"{key} should be uncapped"


def test_the_free_floor_keeps_its_one_domained_site():
    """Free includes a custom domain on ONE site. Repricing must not quietly take
    the acquisition hook away."""
    assert site_plans.get_site_plan("basic").max_domained_sites == 1


# ---------------------------------------------------------------------------
# Purchasability still keys off the price being zero
# ---------------------------------------------------------------------------


def test_a_zero_price_tier_is_always_purchasable(monkeypatch):
    """``purchasable`` is ``price == 0 or a product is configured``. The rename has
    to carry that through, or the free tier becomes unbuyable the moment Dodo is
    unconfigured and every publish falls back to nothing."""
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda key: None)

    assert site_plans.get_site_plan("basic").purchasable is True
    assert site_plans.get_site_plan("pro").purchasable is False


def test_a_priced_tier_is_purchasable_once_it_has_a_product(monkeypatch):
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: "prod_x" if key == "pro" else None
    )

    assert site_plans.get_site_plan("pro").purchasable is True
    assert site_plans.get_site_plan("business").purchasable is False
