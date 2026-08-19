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

from __future__ import annotations

from pocketpaw_ee.cloud.billing import site_plans as catalog
from pocketpaw_ee.cloud.entitlements.dto import site_plan_tier_to_dto
from pocketpaw_ee.cloud.entitlements.service import resolve_site_entitlements

# --------------------------------------------------------------------------- #
# sells_concierge — derived from the floor, not a per-tier mapping
# --------------------------------------------------------------------------- #


def test_base_tier_does_not_sell_the_concierge():
    base = catalog.get_site_plan(catalog.BASE_SITE_PLAN_KEY)
    assert base is not None
    assert base.sells_concierge is False


def test_every_tier_above_the_floor_sells_the_concierge():
    paid = [t for t in catalog.list_site_plans() if t.key != catalog.BASE_SITE_PLAN_KEY]
    assert paid, "catalog has no paid tier — this test would assert nothing"
    assert all(t.sells_concierge for t in paid)


def test_the_rule_follows_the_floor_rather_than_the_tier_name(monkeypatch):
    """Rekeying the floor moves the answer with it — no basic/pro/business mapping.

    This is the property that makes the pricing-spec rekey cheap: point
    ``BASE_SITE_PLAN_KEY`` at another tier and that tier stops selling the
    concierge, with nothing else edited. A per-tier dict would fail here.
    """
    monkeypatch.setattr(catalog, "BASE_SITE_PLAN_KEY", "pro")
    assert catalog.get_site_plan("pro").sells_concierge is False
    assert catalog.get_site_plan("basic").sells_concierge is True


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
    """The free tier's row carries three explicit Falses, not an empty payload.

    Its card is the one that has to render "badge shown", "no concierge", "no
    custom domain" — omissions are only renderable if they arrive as False rather
    than as absent keys.
    """
    dto = site_plan_tier_to_dto(catalog.get_site_plan(catalog.BASE_SITE_PLAN_KEY))
    assert dto.badge_removal is False
    assert dto.sells_concierge is False
    assert dto.cloudflare_features == []


def test_a_paid_tier_card_carries_both_paid_capabilities():
    dto = site_plan_tier_to_dto(catalog.get_site_plan("pro"))
    assert dto.badge_removal is True
    assert dto.sells_concierge is True
    assert "custom_domain" in dto.cloudflare_features
