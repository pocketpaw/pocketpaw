# tests/cloud/billing/test_site_pricing_ladder.py — the per-site price ladder, as
# decided rather than as originally guessed.
#
# The old catalog was $0 / $120 / $480 ANNUAL, and the annual figures had no cost
# basis behind them (see docs/design/drafts/2026-08-21-paw-sites-pricing-revision.md:
# the real Cloudflare floor is $0.10 per custom hostname per month, and every
# comparable product on the market prices monthly in single digits). Annual-only
# billing was also a conversion problem in its own right.
#
# Decided 2026-08-22, in two steps on the same day. First $0 / $5 / $19 monthly on
# the placeholder keys, then the full pricing spec: FIVE tiers on the approved
# ladder, rekeyed off basic/pro/business.
#
#   free    $0    per site (the floor)   badge on, 1 domained site
#   site    $7    per site               badge off, custom domain
#   staff   $19   per site               + the visitor concierge, 200 conv/mo
#   studio  $39   per ORG, flat          white-label across 5 included sites
#   agency  $149  per ORG, flat          25 sites, SSO + SLA, pooled credits
#
# THREE THINGS THIS FILE EXISTS TO CATCH, none of them "is the number right":
#
#   * THE REKEY DEMOTING PRODUCTION. ``Site.plan_tier`` holds basic/pro/business
#     today. An unrecognised key resolves to None, which drops a site to the free
#     floor: badge back, custom domain revoked. The legacy aliases are the only
#     thing standing between the rename and that outcome, so they are asserted by
#     CAPABILITY, not just by resolving to something non-None.
#
#   * (RETIRED 2026-09-05) THE REKEY SILENTLY DISABLING CHECKOUT. The catalog
#     used to resolve a Dodo product per tier, and a rename that missed the
#     legacy env keys made every paid tier unpurchasable while publishes quietly
#     recorded the free floor. There is no gateway in the per-site ladder any
#     more — a site is paid for from the workspace credit balance — so both the
#     ids and that whole failure mode are gone.
#
#   * AN ORG FLAT REACHING A SITE. studio/agency are one subscription covering
#     many sites. Their keys are not legal ``Site.plan_tier`` values, and a plain
#     catalog lookup would happily resolve one — handing a single site the
#     allowance an org buys once for twenty-five.
#
# Created 2026-08-22 (feat/site-pricing-monthly): new test module.
# Updated 2026-08-22 (feat/site-pricing-ladder): rewritten for the five-tier spec
#   ladder. The old file asserted the three placeholder keys and their $0/$5/$19
#   prices; every one of those assertions is now expressed against the current
#   names, plus the three properties above, which did not exist to break before.

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
    [("free", 0), ("site", 7), ("staff", 19)],
)
def test_the_ladder_matches_the_pricing_spec(key, price):
    tier = site_plans.get_site_plan(key)
    assert tier is not None, f"{key} left the catalog"
    assert tier.monthly_price_usd == price


def test_the_price_is_monthly_and_nothing_still_calls_it_annual():
    """The rename is the point, not cosmetics. A field named ``annual_price_usd``
    holding 7 would read as $7/year to every future caller, and $7/year is below
    the Cloudflare floor for a single custom hostname."""
    tier = site_plans.get_site_plan("site")

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


def test_every_tier_carries_the_copy_a_card_needs():
    """The card copy moved server-side in this change, so an unnamed tier renders
    as a bare key. Asserted for EVERY row rather than spot-checked: the failure
    mode is a tier added later with no display entry, which spot-checks miss."""
    for tier in site_plans.list_site_plans():
        assert tier.display_name, f"{tier.key} has no display name"
        assert tier.tagline, f"{tier.key} has no tagline"


# ---------------------------------------------------------------------------
# The retired org flats — a key that LEFT the catalog must still fail closed
# ---------------------------------------------------------------------------


def test_the_catalog_ships_three_per_site_rungs_and_nothing_else():
    """``studio`` and ``agency`` were retired on 2026-09-06.

    The workspace plan carries sites now (Paw Go 1, Pro 3, Pro Max 10), so a
    second ladder selling 5 sites for $39 both contradicted it and priced worse
    than Pro Max's 10 for $49. Neither flat was ever purchasable, so nothing was
    sold on either.
    """
    assert [t.key for t in site_plans.list_site_plans()] == ["free", "site", "staff"]
    # ...and the scoped list agrees, because nothing is org-scoped any more.
    assert [t.key for t in site_plans.list_site_scoped_plans()] == ["free", "site", "staff"]
    assert not any(t.is_org_scoped for t in site_plans.list_site_plans())


@pytest.mark.parametrize("key", ["studio", "agency"])
def test_a_retired_org_key_stored_on_a_site_still_resolves_to_nothing(key):
    """THE PROPERTY THAT HAD TO SURVIVE THE RETIREMENT, and the reason it now
    holds is different from the reason it used to.

    Before, these were real catalog rows that ``site_scoped_tier`` refused by
    scope. Now they are simply gone, so the refusal comes from the unknown-key
    path instead. The ANSWER is what a Site doc holding one of these keys depends
    on — there is no migration, and a document written before today still says
    ``plan_tier: "studio"`` — and the answer must stay None either way, which
    lands that site on the free floor rather than handing it an org allowance.

    Breaks on: adding either key back to the catalog without deciding what a site
    storing it should get.
    """
    assert site_plans.get_site_plan(key) is None
    assert site_plans.site_scoped_tier(key) is None


def test_a_tier_the_catalog_does_not_know_is_built_org_scoped():
    """The fail-closed default, tested where it lives.

    ``_build`` is called directly here and that is not laziness — it is the only
    way to reach the default at all. The public lookups resolve a canonical key
    first and return None for anything else, so ``_SITE_PLAN_SCOPE.get(key,
    ORG_SCOPE)`` never sees an unknown key today. Its whole job is to be correct
    for the caller that eventually does, and a mutation flipping the default to
    ``SITE_SCOPE`` escaped every other test in this file.

    ORG stays the safe default even though no tier is org-scoped any more: it is
    the scope that is NOT a legal ``Site.plan_tier``, so an unknown key fails
    closed OUT of the per-site path rather than into it.

    Breaks on: changing that default to ``SITE_SCOPE``.
    """
    unknown = site_plans._build("tier-that-does-not-exist")

    assert unknown.is_org_scoped is True
    assert unknown.monthly_price_usd == 0
    assert unknown.badge_removal is False
    assert unknown.max_domained_sites == 0
    assert unknown.purchasable is False


# ---------------------------------------------------------------------------
# The rekey — legacy keys must keep resolving, by capability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "current"),
    [("basic", "free"), ("pro", "site"), ("business", "staff")],
)
def test_a_legacy_key_resolves_to_the_tier_with_the_same_capabilities(legacy, current):
    """Not merely "resolves to something" — to the tier that sells what the old one
    sold.

    ``pro`` sold badge removal and uncapped domains and NO concierge; ``site``
    does exactly that. ``business`` added the concierge; ``staff`` does. Mapping
    by ladder POSITION over the five-rung catalog instead would have put
    ``business`` on ``studio``, which is an org flat and not even a legal value
    for the field these keys are stored in.

    Breaks on: deleting an alias (the tier resolves to None and every already-
    published site on it drops to the free floor), or repointing one at the wrong
    rung.
    """
    resolved = site_plans.get_site_plan(legacy)
    expected = site_plans.get_site_plan(current)

    assert resolved is not None, f"{legacy} no longer resolves — production sites hold this key"
    assert resolved.key == current, "the resolved row must report its CURRENT name"
    assert resolved.badge_removal == expected.badge_removal
    assert resolved.sells_concierge == expected.sells_concierge
    assert resolved.max_domained_sites == expected.max_domained_sites
    assert resolved.monthly_price_usd == expected.monthly_price_usd


def test_a_legacy_key_is_still_a_site_scoped_tier():
    """The aliases have to survive the guarded read too, not just the plain one —
    ``site_scoped_tier`` is what every entitlement seam calls, so an alias that
    resolved only through ``get_site_plan`` would still demote every live site."""
    for legacy in ("basic", "pro", "business"):
        assert site_plans.site_scoped_tier(legacy) is not None


def test_an_unknown_key_still_resolves_to_nothing():
    """The aliases must not turn the lookup into "always find something"."""
    assert site_plans.get_site_plan("nonesuch") is None
    assert site_plans.get_site_plan("") is None
    assert site_plans.get_site_plan(None) is None
    assert site_plans.canonical_site_tier_key("nonesuch") is None


# ---------------------------------------------------------------------------
# The rekey must not break the DEPLOYED Dodo product map
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The concierge is the difference between the two paid PER-SITE rungs
# ---------------------------------------------------------------------------


def test_only_the_top_per_site_rung_sells_the_concierge():
    """It used to be derived as "any tier above the floor", which was correct only
    while the tier meant to sell it did not exist. Deriving it now would hand the
    $7 rung the feature the $19 rung is for.

    Scoped to the per-site rungs deliberately: ``agency`` also sells the
    concierge, legitimately, and a whole-catalog assertion would have to list it
    — at which point the assertion stops saying anything about ``site``.
    """
    by_key = _by_key()

    assert by_key["free"].sells_concierge is False
    assert by_key["site"].sells_concierge is False, (
        "the $7 tier is selling the concierge, which is what the $19 tier is for"
    )
    assert by_key["staff"].sells_concierge is True


def test_the_conversation_allowance_belongs_to_the_tier_that_sells_the_concierge():
    by_key = _by_key()

    assert by_key["staff"].conversation_allowance == 200
    assert by_key["site"].conversation_allowance == 0
    assert by_key["free"].conversation_allowance == 0


def test_the_conversation_rate_is_cents_and_stays_an_int():
    """This asserted ``agency``'s $0.05 against the $0.10 list rate until that
    tier was retired. What it was really guarding outlives the tier: the rate is
    in CENTS, and a float rate multiplied by a conversation count is a rounding
    bug waiting for a big enough customer."""
    for tier in site_plans.list_site_plans():
        assert isinstance(tier.conversation_rate_cents, int)
    assert _by_key()["staff"].conversation_rate_cents == 10


def test_a_tier_that_sells_no_concierge_still_carries_the_list_rate():
    """0 would render as "free conversations" on a card. The truth is that the
    tier gets none at all, which ``conversation_allowance == 0`` already says."""
    by_key = _by_key()

    for key in ("free", "site"):
        assert by_key[key].conversation_rate_cents > 0


def test_the_paid_rungs_are_actually_different_products():
    """If the two paid per-site tiers sold exactly the same things, the ladder
    would be a price increase with no reason attached to it."""
    by_key = _by_key()
    mid, top = by_key["site"], by_key["staff"]

    assert (mid.sells_concierge, mid.badge_removal) != (top.sells_concierge, top.badge_removal) or (
        mid.cloudflare_features != top.cloudflare_features
    )


def test_both_paid_rungs_still_drop_the_badge_and_take_a_domain():
    """The $7 rung's whole pitch. Losing either while repricing would be a silent
    downgrade for anyone who buys it."""
    by_key = _by_key()

    for key in ("site", "staff"):
        assert by_key[key].badge_removal is True
        assert by_key[key].max_domained_sites is None, f"{key} should be uncapped"


def test_the_free_floor_keeps_its_one_domained_site():
    """Free includes a custom domain on ONE site. This is the one place the
    catalog knowingly departs from the written pricing spec, which says
    subdomain-only — the captain's call, and the acquisition hook. Repricing must
    not quietly take it away."""
    assert site_plans.get_site_plan("free").max_domained_sites == 1


def test_highlights_stay_out_of_the_enforced_feature_set():
    """SSO and an SLA are commitments a human honours, not flags code checks.
    Folded into ``cloudflare_features`` they would render as an enforced
    inclusion beside the WAF, which nothing would be enforcing.

    No rung ships highlights since the org flats were retired — they were the only
    tiers that had any. The separation is still asserted rather than deleted,
    because the next tier to sell an unenforceable promise will reach for this
    field, and the mistake it guards against is putting the promise in the other
    one."""
    for tier in site_plans.list_site_plans():
        features = {f.lower() for f in tier.cloudflare_features}
        for claim in tier.highlights:
            assert claim.lower() not in features
        assert "sso" not in features


# ---------------------------------------------------------------------------
# Purchasability still keys off the price being zero
# ---------------------------------------------------------------------------


def test_every_per_site_rung_is_purchasable():
    """``purchasable`` is now ``scope == "site"``, and this is the case that pins
    it. It used to be ``price == 0 or a product is configured``, which meant a
    deployment with no Dodo map — the ordinary state of a self-hosted install —
    offered a ladder where only the free rung could be selected.

    A paid site is bought from the workspace credit wallet now, so the gateway
    map has nothing to say about whether a rung is on sale. Every rung is,
    including with the product resolver stubbed to return nothing at all."""
    assert site_plans.get_site_plan("free").purchasable is True
    assert site_plans.get_site_plan("site").purchasable is True
    assert site_plans.get_site_plan("staff").purchasable is True


def test_purchasable_still_answers_by_scope_rather_than_configuration():
    """``purchasable`` outlived the rows that motivated it, and this pins why.

    It used to read "is a Dodo product configured", which made a self-hosted
    install — the ordinary case — offer a ladder where only the free rung could be
    selected. It reads ``scope == "site"`` now, so every shipped rung is buyable
    and a key that fails closed to org scope is not.
    """
    for tier in site_plans.list_site_plans():
        assert tier.purchasable is True, f"{tier.key} should be buyable one site at a time"
    assert site_plans._build("tier-that-does-not-exist").purchasable is False
