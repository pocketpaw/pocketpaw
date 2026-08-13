# tests/cloud/entitlements/test_site_entitlements.py — a site's paid capabilities
# need a subscription that is actually paying.
# Created 2026-08-13 (feat/sites-site-entitlements). The hole this pins shut: a
# per-site plan records ``plan_tier`` and NOTHING ever resets it. Cancellation
# sets ``subscription_status="cancelled"`` and leaves the tier on the paid key,
# and — wider — an unconfigured Dodo product lets a paid publish record its tier
# with no live charge at all (``subscription_status="none"``). Any resolver that
# reads the tier alone therefore hands a free badge removal and a free custom
# domain to sites that have never paid, permanently.
#
# The resolver is pure, so every branch is exercised without a database: it takes
# the site's billing fields because ``entitlements`` may not import ``models.site``
# (EE cloud rule 2).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.billing import site_plans as site_plan_catalog
from pocketpaw_ee.cloud.entitlements.service import resolve_site_entitlements

_SITE = "6512c1f0e4b0a1b2c3d4e5f6"
_WS = "ws_1"


def _resolve(**ov):
    kw = {
        "site_id": _SITE,
        "workspace_id": _WS,
        "plan_tier": "pro",
        "subscription_status": "active",
        "concierge_enabled": True,
    }
    kw.update(ov)
    return resolve_site_entitlements(**kw)


# --------------------------------------------------------------------------- #
# The paying case
# --------------------------------------------------------------------------- #


def test_an_active_paid_site_drops_the_badge_and_gets_its_domain():
    ent = _resolve(plan_tier="pro", subscription_status="active")

    assert ent.subscription_active is True
    assert ent.badge_required is False
    assert ent.custom_domain is True
    assert ent.plan_tier == "pro"


def test_business_is_paid_too():
    ent = _resolve(plan_tier="business", subscription_status="active")

    assert ent.badge_required is False
    assert ent.custom_domain is True


# --------------------------------------------------------------------------- #
# The hole: a paid TIER without a paying SUBSCRIPTION
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["cancelled", "none", "pending", "", None, "garbage"])
def test_a_paid_tier_without_an_active_subscription_is_badged(status):
    """The whole reason this resolver exists. ``plan_tier`` stays "pro" through
    cancellation — nothing resets it — so gating on the tier alone means a
    cancelled site never sees a badge again."""
    ent = _resolve(plan_tier="pro", subscription_status=status)

    assert ent.subscription_active is False
    assert ent.badge_required is True
    assert ent.custom_domain is False


def test_a_tier_recorded_but_never_charged_is_badged():
    """Today's live case, not a hypothetical: no Dodo product is configured, so a
    paid publish records the intended tier and takes no money —
    ``subscription_status`` stays "none"."""
    ent = _resolve(plan_tier="business", subscription_status="none")

    assert ent.badge_required is True
    assert ent.custom_domain is False


def test_the_tier_is_still_reported_when_it_is_not_being_paid_for():
    """Report what the row says, gate on whether it is paid. Blanking the tier
    would lose the information a reconcile/dunning view needs."""
    ent = _resolve(plan_tier="pro", subscription_status="cancelled")

    assert ent.plan_tier == "pro"
    assert ent.subscription_active is False


# --------------------------------------------------------------------------- #
# Fail-closed on the base and the unknown
# --------------------------------------------------------------------------- #


def test_the_base_tier_is_badged_even_when_active():
    ent = _resolve(plan_tier="basic", subscription_status="active")

    assert ent.badge_required is True
    assert ent.custom_domain is False


@pytest.mark.parametrize("tier", [None, "", "stduio", "site", "staff"])
def test_an_unknown_or_absent_tier_falls_to_the_base(tier):
    """Includes the plan ids the pricing spec proposes but the catalog does not
    carry yet — until they are added, they must resolve to badged, not to a
    silent upgrade."""
    ent = _resolve(plan_tier=tier, subscription_status="active")

    assert ent.plan_tier == site_plan_catalog.BASE_SITE_PLAN_KEY
    assert ent.badge_required is True
    assert ent.custom_domain is False


# --------------------------------------------------------------------------- #
# Passthrough + scope
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("enabled", [True, False])
def test_concierge_passes_through_untouched(enabled):
    """The owner's kill switch is not a billing decision — it is carried, not
    re-decided, so a paid site can still turn its own concierge off."""
    assert _resolve(concierge_enabled=enabled).concierge_enabled is enabled


def test_the_scope_travels_with_the_answer():
    ent = _resolve()

    assert ent.site_id == _SITE
    assert ent.workspace_id == _WS


def test_the_blocked_flags_are_absent_rather_than_stubbed():
    """conv_allowance / conv_rate_usd / white_label wait on decisions that are
    still open. A field that always returned 0/False would read as implemented."""
    ent = _resolve()

    for absent in ("conv_allowance", "conv_rate_usd", "white_label"):
        assert not hasattr(ent, absent)
