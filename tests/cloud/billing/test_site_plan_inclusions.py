# tests/cloud/billing/test_site_plan_inclusions.py — the plan CARD must be able to
# say what a tier includes, and what it leaves out.
#
# Created 2026-08-19 (feat/site-plan-catalog-inclusions). What this pins: two
# capabilities the paid tiers are actually sold on — dropping the attribution
# badge, and running a visitor concierge — existed server-side and reached no
# client. ``GET /billing/site-plans`` shipped ``cloudflare_features`` only, so the
# buyer-facing cards rendered a Cloudflare feature list and said nothing about
# either. The free tier's card listed NOTHING at all, which is the worst case: the
# one tier whose omissions are the entire product.
#
# Two properties are asserted, not one:
#   * the catalog's ``sells_concierge`` is derived from the FLOOR, so the rekey in
#     step 3 of the pricing spec (basic/pro/business -> free/site/staff) does not
#     have to rewrite it;
#   * the DTO mirrors the catalog row EXACTLY for every tier, so a tier added to
#     the catalog cannot reach the wire with these two silently False.
#
# The second is the one worth having. A hand-written "pro is True" assertion
# passes just as well against a mapper that hardcodes the answer.
#
# Updated 2026-09-08 (fix/sites-plan-domain-allowance): added the
# ``max_domained_sites`` section at the bottom, and corrected a docstring that had
# gone stale. Same failure as the original, one field later: custom-domain
# entitlement moved to ``max_domained_sites`` on 2026-08-21 and never reached the
# wire, so the cards kept answering "custom domain?" from ``cloudflare_features``.
# The two disagree on the FREE FLOOR — one domained site, zero resold Cloudflare
# features — so the tier a buyer reads first is the tier the card got wrong. The
# last test asserts the DTO against ``site_domain_allowance`` rather than a
# literal, because a card promising what the gate refuses is the real thing to
# prevent.

from __future__ import annotations

from pocketpaw_ee.cloud.billing import site_plans as catalog
from pocketpaw_ee.cloud.entitlements.dto import site_plan_tier_to_dto
from pocketpaw_ee.cloud.entitlements.service import (
    resolve_site_entitlements,
    site_domain_allowance,
)

# --------------------------------------------------------------------------- #
# sells_concierge — derived from the floor, not a per-tier mapping
# --------------------------------------------------------------------------- #


def test_base_tier_does_not_sell_the_concierge():
    base = catalog.get_site_plan(catalog.BASE_SITE_PLAN_KEY)
    assert base is not None
    assert base.sells_concierge is False


def test_only_the_top_per_site_rung_sells_the_concierge():
    """The concierge is what separates the two PAID PER-SITE rungs, so the cheaper
    one must not have it.

    Written against ``list_site_scoped_plans`` rather than the whole catalog, and
    that is the point of the 2026-08-22 edit rather than a detail of it. The old
    assertion was ``selling == [tiers[-1].key]`` — "only the last row in the
    catalog" — which was a statement about POSITION that happened to coincide with
    the rule while the catalog was one ladder. The five-tier catalog held two:
    the per-site rungs (free/site/staff) and the org flats (studio/agency), and
    ``agency`` legitimately sold the concierge too. Under the old wording that read
    as a violation, and "fixing" it by appending agency to the expected list would
    have quietly permitted the actual regression this test exists to catch —
    ``site`` gaining a ``staff`` feature.

    The flats were retired on 2026-09-06 and the catalog is one ladder again, so
    position and rule coincide once more. THE POSITIONAL WORDING IS STILL WRONG,
    and is not restored: it would pass for the wrong reason today and break again
    the next time a tier is added anywhere but the end.

    So the rule is asserted where it lives: among the rungs a single site can be
    put on, exactly one sells the concierge, and it is the most expensive one.
    """
    rungs = catalog.list_site_scoped_plans()
    assert len(rungs) > 2, "needs at least two paid rungs to say anything"

    selling = [t.key for t in rungs if t.sells_concierge]
    assert selling == [rungs[-1].key], (
        f"expected only the top per-site rung to sell the concierge, got {selling}"
    )
    # And the rung below it must be a PAID one — otherwise "only the top rung"
    # is satisfied by a two-rung ladder of free + one paid tier, where there is
    # no cheaper paid rung to wrongly grant anything to.
    assert rungs[-2].monthly_price_usd > 0
    assert rungs[-2].sells_concierge is False


def test_an_unknown_tier_sells_no_concierge():
    """The mapping fails CLOSED.

    This replaces a test asserting the opposite property — that the answer
    followed ``BASE_SITE_PLAN_KEY`` rather than the tier name, which was the thing
    that made the derivation cheap. That stopped being true on 2026-08-22: the
    concierge is now mapped per tier, because it is what separates the $5 rung
    from the $19 one and a floor comparison cannot express that.

    The cost of the map is that a new tier has to be added to it. Failing closed
    is what makes forgetting safe: an unmapped tier sells nothing rather than
    silently selling the most expensive feature in the catalog.
    """
    assert catalog.get_site_plan("nonesuch-tier") is None
    assert catalog._SITE_PLAN_SELLS_CONCIERGE.get("nonesuch-tier", False) is False


def test_the_resolver_reads_the_catalog_rule_rather_than_its_own(monkeypatch):
    """``resolve_site_entitlements`` must not re-express the floor comparison.

    One rule written twice is one rule that drifts. Overriding the property is
    what makes this discriminating: moving ``BASE_SITE_PLAN_KEY`` instead would
    pass against the old inline ``tier.key != BASE_SITE_PLAN_KEY`` too, since that
    reads the same global. Only a resolver that actually calls ``sells_concierge``
    follows the property here.

    Breaks on: restoring the inline comparison in ``resolve_site_entitlements``.
    """
    monkeypatch.setattr(catalog.SitePlanTier, "sells_concierge", property(lambda _self: False))
    ent = resolve_site_entitlements(
        site_id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace_id="ws_1",
        plan_tier="pro",
        subscription_status="active",
        concierge_enabled=True,
    )
    assert ent.concierge_entitled is False


# --------------------------------------------------------------------------- #
# The wire — the whole point of the change
# --------------------------------------------------------------------------- #


def test_the_dto_mirrors_every_catalog_row():
    """No tier reaches the wire with these two silently False.

    Written as a sweep over the catalog rather than three hand-written rows: a
    mapper that hardcodes ``badge_removal=False`` passes a per-tier assertion for
    the free tier and fails here.
    """
    for tier in catalog.list_site_plans():
        dto = site_plan_tier_to_dto(tier)
        assert dto.badge_removal is tier.badge_removal, tier.key
        assert dto.sells_concierge is tier.sells_concierge, tier.key


def test_the_free_tier_card_can_say_what_it_omits():
    """The free tier's row carries explicit Falses, not an empty payload.

    Its card is the one that has to render "badge shown" and "no concierge" —
    omissions are only renderable if they arrive as False rather than as absent
    keys.

    IT DOES NOT OMIT THE CUSTOM DOMAIN, and this docstring used to say it did.
    That was written on 2026-08-19, two days before the floor grant landed. An
    empty ``cloudflare_features`` is not that claim: see
    ``test_the_free_tier_ships_its_domain_grant_on_the_wire`` below.
    """
    dto = site_plan_tier_to_dto(catalog.get_site_plan(catalog.BASE_SITE_PLAN_KEY))
    assert dto.badge_removal is False
    assert dto.sells_concierge is False
    assert dto.cloudflare_features == []


def test_a_paid_tier_card_carries_its_paid_capabilities():
    """The $5 rung sells the badge removal and the domain, not the concierge."""
    dto = site_plan_tier_to_dto(catalog.get_site_plan("pro"))
    assert dto.badge_removal is True
    assert dto.sells_concierge is False
    assert "custom_domain" in dto.cloudflare_features


def test_the_top_tier_card_is_the_one_carrying_the_concierge():
    dto = site_plan_tier_to_dto(catalog.get_site_plan("business"))
    assert dto.sells_concierge is True
    assert dto.badge_removal is True


# --------------------------------------------------------------------------- #
# max_domained_sites — the floor grant the wire could not express
# --------------------------------------------------------------------------- #


def test_the_dto_mirrors_the_domain_allowance_for_every_tier():
    """A sweep, for the same reason the badge sweep above is one.

    Breaks on: a mapper that drops the field, or one that reintroduces the card's
    old derivation by sending ``"custom_domain" in cloudflare_features`` instead.
    """
    for tier in catalog.list_site_plans():
        dto = site_plan_tier_to_dto(tier)
        assert dto.max_domained_sites == tier.max_domained_sites, tier.key


def test_the_free_tier_ships_its_domain_grant_on_the_wire():
    """FREE INCLUDES A CUSTOM DOMAIN, and its card has to be able to say so.

    This is the assertion the storefront was missing a field for. The floor grants
    one domained site with no subscription (captain, 2026-08-21) while reselling no
    Cloudflare features at all, because that collection means only RESOLD
    Cloudflare capability. A card reading the feature list to answer "custom
    domain?" therefore printed an X on the one tier that gets one for nothing.

    The two assertions together are the point: an empty feature list AND a
    non-zero allowance, on the same row.
    """
    dto = site_plan_tier_to_dto(catalog.get_site_plan(catalog.BASE_SITE_PLAN_KEY))
    assert dto.cloudflare_features == []
    assert dto.max_domained_sites == 1


def test_a_paid_rung_ships_an_uncapped_allowance():
    """``None`` is uncapped, and it must survive the mapper as ``None``.

    Coerced to 0 on the way out it would read as "no domains" — the opposite
    claim — on the cheapest tier that actually sells them.
    """
    dto = site_plan_tier_to_dto(catalog.get_site_plan("site"))
    assert dto.max_domained_sites is None


def test_the_wire_allowance_agrees_with_the_resolver():
    """The card and the gate must not answer the same question differently.

    ``site_domain_allowance`` is what actually decides whether a site may attach a
    domain. A card built off this DTO promises what that resolves — checked here
    for the floor with no subscription at all, which is the case the old card got
    wrong.
    """
    dto = site_plan_tier_to_dto(catalog.get_site_plan(catalog.BASE_SITE_PLAN_KEY))
    assert dto.max_domained_sites == site_domain_allowance(
        plan_tier=None, subscription_status=None
    )
