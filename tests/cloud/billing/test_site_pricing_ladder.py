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
#   * THE REKEY SILENTLY DISABLING CHECKOUT. ``POCKETPAW_DODO_SITE_PRODUCTS`` is
#     deployed keyed {"pro": ..., "business": ...}. A catalog that only looked up
#     the new names would find nothing, every paid tier would go unpurchasable,
#     and publishes would quietly record the free floor while taking no money.
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
    [("free", 0), ("site", 7), ("staff", 19), ("studio", 39), ("agency", 149)],
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
# The two scopes — the org flats must never become a site's own plan
# ---------------------------------------------------------------------------


def test_the_catalog_holds_both_scopes_and_says_which_is_which():
    by_key = _by_key()

    assert [t.key for t in site_plans.list_site_scoped_plans()] == ["free", "site", "staff"]
    assert [t.key for t in site_plans.list_site_plans() if t.is_org_scoped] == [
        "studio",
        "agency",
    ]
    assert by_key["staff"].is_org_scoped is False
    assert by_key["studio"].is_org_scoped is True


@pytest.mark.parametrize("key", ["studio", "agency"])
def test_an_org_flat_is_refused_as_a_sites_own_tier(key):
    """The security property of the whole two-scope change.

    ``get_site_plan`` resolves it — it is a real catalog row and the storefront
    renders it. ``site_scoped_tier`` must NOT, because every entitlement seam
    reads a site's stored ``plan_tier`` through that function, and an org key
    resolving there hands one site the white-label allowance an org pays for
    across its whole estate.

    Breaks on: ``site_scoped_tier`` losing its ``is_org_scoped`` check, or any
    entitlement seam being switched back to ``get_site_plan``.
    """
    assert site_plans.get_site_plan(key) is not None
    assert site_plans.site_scoped_tier(key) is None


@pytest.mark.parametrize("key", ["studio", "agency"])
def test_an_org_flat_is_never_purchasable_however_config_is_set(key, monkeypatch):
    """Not "has no product configured" — NEVER, whatever config says.

    There is exactly one checkout and it buys one site. Pointing it at a $149
    org flat would charge the org price and grant a single site's capability.
    So the refusal cannot be left to "nobody configured a product for it": a
    well-meaning operator filling in every key of POCKETPAW_DODO_SITE_PRODUCTS
    would otherwise switch on a checkout that overcharges.

    Breaks on: ``purchasable`` dropping its ``is_org_scoped`` early return.
    """
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda _k: "pdt_configured")

    assert site_plans.get_site_plan(key).purchasable is False
    # ...while a per-site rung with the same product IS purchasable, so the test
    # is measuring the scope and not a blanket "nothing is purchasable".
    assert site_plans.get_site_plan("site").purchasable is True


def test_a_tier_the_catalog_does_not_know_is_built_org_scoped():
    """The fail-closed default, tested where it lives.

    ``_build`` is called directly here and that is not laziness — it is the only
    way to reach the default at all. The public lookups resolve a canonical key
    first and return None for anything else, so ``_SITE_PLAN_SCOPE.get(key,
    ORG_SCOPE)`` never sees an unknown key today. Its whole job is to be correct
    for the caller that eventually does, and a mutation flipping the default to
    ``SITE_SCOPE`` escaped every other test in this file.

    ORG is the safe default precisely because it is the scope that is NOT a legal
    ``Site.plan_tier``: an unknown key fails closed OUT of the per-site path
    rather than into it, and ``site_scoped_tier`` then refuses it.

    Breaks on: changing that default to ``SITE_SCOPE``.
    """
    unknown = site_plans._build("tier-that-does-not-exist")

    assert unknown.is_org_scoped is True
    assert unknown.monthly_price_usd == 0
    assert unknown.badge_removal is False
    assert unknown.max_domained_sites == 0
    assert unknown.purchasable is False


def test_only_the_org_flats_include_a_site_count():
    """``included_sites`` answers "how many sites does this flat cover", which is
    not a question a per-site subscription has an answer to. A number there would
    read as a per-site quota."""
    by_key = _by_key()

    assert by_key["studio"].included_sites == 5
    assert by_key["agency"].included_sites == 25
    for key in ("free", "site", "staff"):
        assert by_key[key].included_sites is None, f"{key} should not include a site count"


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


def test_the_deployed_legacy_product_map_still_opens_a_checkout(monkeypatch):
    """The trap that would have shipped silently.

    The live environment sets POCKETPAW_DODO_SITE_PRODUCTS keyed by the OLD tier
    names. Renaming the catalog without teaching ``_dodo_product_for`` about the
    aliases means every paid tier comes back ``purchasable = False`` the moment
    the rename deploys — no checkout opens, and ``publish_pocket`` falls back to
    publishing live and recording the free floor. Nothing errors. The customer
    picks a paid plan, is charged nothing, and gets nothing.

    Breaks on: ``_dodo_product_for`` looking up only the canonical key.
    """

    class _Settings:
        dodo_site_products = {"pro": "pdt_old_site", "business": "pdt_old_staff"}

    import pocketpaw.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())

    assert site_plans.get_site_plan("site").dodo_product_id == "pdt_old_site"
    assert site_plans.get_site_plan("staff").dodo_product_id == "pdt_old_staff"
    assert site_plans.get_site_plan("site").purchasable is True


def test_the_new_key_wins_over_a_legacy_one_for_the_same_tier(monkeypatch):
    """A half-migrated environment holding both must not be ambiguous — the
    canonical key is the answer, so re-keying the env var takes effect
    immediately rather than being shadowed by the value it replaces."""

    class _Settings:
        dodo_site_products = {"site": "pdt_new", "pro": "pdt_old"}

    import pocketpaw.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())

    assert site_plans.get_site_plan("site").dodo_product_id == "pdt_new"


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


def test_the_agency_rate_is_the_discount_it_is_sold_on():
    """$0.05 against a $0.10 list rate is half the headline reason to buy agency.
    In CENTS — a float rate multiplied by a conversation count is a rounding bug
    waiting for a big enough customer."""
    by_key = _by_key()

    assert by_key["agency"].conversation_rate_cents == 5
    assert by_key["staff"].conversation_rate_cents == 10
    assert isinstance(by_key["agency"].conversation_rate_cents, int)


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


def test_white_label_is_what_separates_an_org_flat_from_five_site_subscriptions():
    by_key = _by_key()

    assert by_key["studio"].white_label is True
    assert by_key["agency"].white_label is True
    for key in ("free", "site", "staff"):
        assert by_key[key].white_label is False


def test_highlights_are_kept_out_of_the_enforced_feature_set():
    """SSO and an SLA are commitments a human honours, not flags code checks.
    Folded into ``cloudflare_features`` they would render as an enforced
    inclusion beside the WAF, which nothing would be enforcing."""
    agency = _by_key()["agency"]

    assert agency.highlights, "agency's selling points vanished"
    for claim in agency.highlights:
        assert claim.lower() not in {f.lower() for f in agency.cloudflare_features}
    assert "sso" not in {f.lower() for f in agency.cloudflare_features}


# ---------------------------------------------------------------------------
# Purchasability still keys off the price being zero
# ---------------------------------------------------------------------------


def test_a_zero_price_tier_is_always_purchasable(monkeypatch):
    """``purchasable`` is ``price == 0 or a product is configured``. The rename has
    to carry that through, or the free tier becomes unbuyable the moment Dodo is
    unconfigured and every publish falls back to nothing."""
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda key: None)

    assert site_plans.get_site_plan("free").purchasable is True
    assert site_plans.get_site_plan("site").purchasable is False


def test_a_priced_tier_is_purchasable_once_it_has_a_product(monkeypatch):
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: "prod_x" if key == "site" else None
    )

    assert site_plans.get_site_plan("site").purchasable is True
    assert site_plans.get_site_plan("staff").purchasable is False
