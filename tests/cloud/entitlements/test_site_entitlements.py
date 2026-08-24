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
#
# Updated 2026-08-21 (feat/site-free-custom-domain, PW-1). Several assertions here
# INVERTED, and the inversion is the change, not a regression: free now includes a
# custom domain ("only 1 site is allowed to have a custom domain in free" —
# captain, 2026-08-21), so a site resolving to the floor gets ``custom_domain
# True`` and ``max_domained_sites == 1`` where it used to get False and nothing.
# Badge removal, the concierge, and the UNCAPPED allowance are untouched: those are
# still paid grants and still need an active subscription, which is why the
# badge half of every one of these tests still reads exactly as it did.
#
# ``custom_domain`` answers "may this site have one at all" and is now True almost
# everywhere. The question with teeth moved to the attach seam, which counts SITES
# already holding a domain — see tests/cloud/sites/test_custom_domain_entitlement.py.

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
        "plan_tier": "site",
        "subscription_status": "active",
        "concierge_enabled": True,
    }
    kw.update(ov)
    return resolve_site_entitlements(**kw)


# --------------------------------------------------------------------------- #
# The paying case
# --------------------------------------------------------------------------- #


def test_an_active_paid_site_drops_the_badge_and_gets_its_domain():
    ent = _resolve(plan_tier="site", subscription_status="active")

    assert ent.subscription_active is True
    assert ent.badge_required is False
    assert ent.custom_domain is True
    assert ent.plan_tier == "site"


def test_staff_is_paid_too():
    ent = _resolve(plan_tier="staff", subscription_status="active")

    assert ent.badge_required is False
    assert ent.custom_domain is True


def test_what_a_paying_site_actually_buys_is_an_UNCAPPED_allowance():
    """Free includes a custom domain, so the paid tiers no longer sell the
    capability — they sell the absence of a ceiling on it. None means uncapped."""
    assert _resolve(plan_tier="site", subscription_status="active").max_domained_sites is None


# --------------------------------------------------------------------------- #
# The hole: a paid TIER without a paying SUBSCRIPTION
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["cancelled", "none", "pending", "", None, "garbage"])
def test_a_paid_tier_without_an_active_subscription_is_badged(status):
    """The whole reason this resolver exists. ``plan_tier`` stays on the paid key through
    cancellation — nothing resets it — so gating on the tier alone means a
    cancelled site never sees a badge again."""
    ent = _resolve(plan_tier="site", subscription_status=status)

    assert ent.subscription_active is False
    assert ent.badge_required is True


@pytest.mark.parametrize("status", ["cancelled", "none", "pending", "", None, "garbage"])
def test_a_lapsed_paid_site_falls_to_the_FLOOR_allowance_not_to_zero(status):
    """It keeps what free would have given it, and no more.

    Worth its own test because "fail closed" reads like it should mean zero here,
    and zero would be wrong: a customer who once paid must not end up worse off
    than one who never did. What lapsing actually costs is the UNCAPPED allowance —
    the site drops from None to the floor's 1.
    """
    floor = site_plan_catalog.get_site_plan(site_plan_catalog.BASE_SITE_PLAN_KEY)

    ent = _resolve(plan_tier="site", subscription_status=status)

    assert ent.max_domained_sites == floor.max_domained_sites
    assert ent.custom_domain is True


def test_a_tier_recorded_but_never_charged_is_badged():
    """Today's live case, not a hypothetical: no Dodo product is configured, so a
    paid publish records the intended tier and takes no money —
    ``subscription_status`` stays "none"."""
    ent = _resolve(plan_tier="staff", subscription_status="none")

    assert ent.badge_required is True


def test_the_tier_is_still_reported_when_it_is_not_being_paid_for():
    """Report what the row says, gate on whether it is paid. Blanking the tier
    would lose the information a reconcile/dunning view needs."""
    ent = _resolve(plan_tier="site", subscription_status="cancelled")

    assert ent.plan_tier == "site"
    assert ent.subscription_active is False


# --------------------------------------------------------------------------- #
# Fail-closed on the base and the unknown
# --------------------------------------------------------------------------- #


def test_the_base_tier_is_badged_even_when_active():
    ent = _resolve(plan_tier="free", subscription_status="active")

    assert ent.badge_required is True


def test_the_floor_grants_its_domain_with_no_subscription_at_all():
    """The captain's rule, at the resolver.

    This is the case the old single-branch resolver could not express: every
    capability sat behind ``tier is not None and subscription_active``, so a $0
    tier fell through to all-False and a catalog edit granting free a domain would
    have granted nothing. Splitting floor grants out of paid grants is what makes
    the catalog value reachable.
    """
    ent = _resolve(plan_tier=site_plan_catalog.BASE_SITE_PLAN_KEY, subscription_status="none")

    assert ent.subscription_active is False
    assert ent.max_domained_sites == 1
    assert ent.custom_domain is True


def test_the_allowance_the_resolver_reports_is_the_one_in_the_catalog():
    """Pinned against the catalog, not the literal 1, so the rekey moves this test
    with the ladder instead of breaking it."""
    floor = site_plan_catalog.get_site_plan(site_plan_catalog.BASE_SITE_PLAN_KEY)

    ent = _resolve(plan_tier=site_plan_catalog.BASE_SITE_PLAN_KEY, subscription_status="none")

    assert ent.max_domained_sites == floor.max_domained_sites


def test_a_free_site_still_carries_its_badge_and_still_has_no_concierge():
    """The floor grant is one capability, not a general amnesty. Free getting a
    custom domain must not leak badge removal or the concierge along with it."""
    ent = _resolve(plan_tier=site_plan_catalog.BASE_SITE_PLAN_KEY, subscription_status="none")

    assert ent.badge_required is True
    assert ent.concierge_entitled is False


@pytest.mark.parametrize("tier", [None, "", "stduio", "stafff", "enterprise"])
def test_an_unknown_or_absent_tier_falls_to_the_base(tier):
    """Typos and tiers from other ladders resolve to badged, never to a silent
    upgrade.

    The parameters used to include ``site`` and ``staff``, which were then the
    pricing spec's PROPOSED names and not in the catalog. They are real tiers now,
    so keeping them here would have asserted that the two tiers this change adds
    grant nothing — a test passing for the exact reason the feature was broken.
    They are replaced with near-misses (``stafff``) and a key from the WORKSPACE
    plan ladder (``enterprise``), which is the realistic way a wrong string
    arrives: the two catalogs are different and a caller can reach for the wrong
    one.
    """
    ent = _resolve(plan_tier=tier, subscription_status="active")

    assert ent.plan_tier == site_plan_catalog.BASE_SITE_PLAN_KEY
    assert ent.badge_required is True
    # Fail-closed on the ALLOWANCE too: an unknown tier gets the floor's, never the
    # uncapped one. "active" here is a red herring — there is no tier to activate.
    assert ent.max_domained_sites == 1


@pytest.mark.parametrize("org_key", ["studio", "agency"])
def test_an_org_flat_stored_on_a_site_grants_that_site_nothing(org_key):
    """The security property of the two-scope catalog, at the resolver.

    ``studio`` and ``agency`` are real, resolvable catalog rows that sell
    white-label and badge removal across a whole workspace. They are NOT legal
    ``Site.plan_tier`` values, and this is what happens when one shows up there
    anyway — a hand-edited document, a replayed webhook, a restored backup: the
    site is treated as having no plan of its own and lands on the free floor.

    Note the ``active`` status. That is deliberate: the tier grants badge removal
    and the subscription says paid, so every gate this resolver has would open if
    the key were accepted. The ONLY thing refusing is the scope check.

    Breaks on: ``resolve_site_entitlements`` reading ``get_site_plan`` instead of
    ``site_scoped_tier``.
    """
    assert site_plan_catalog.get_site_plan(org_key) is not None, "the row must still exist"
    assert site_plan_catalog.get_site_plan(org_key).badge_removal is True, (
        "if the org tier stopped selling badge removal this test would pass for the wrong reason"
    )

    ent = _resolve(plan_tier=org_key, subscription_status="active")

    assert ent.plan_tier == site_plan_catalog.BASE_SITE_PLAN_KEY
    assert ent.badge_required is True
    assert ent.concierge_entitled is False
    assert ent.max_domained_sites == 1


# --------------------------------------------------------------------------- #
# The rekey — documents written before 2026-08-22 hold the old keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("legacy", "current"), [("basic", "free"), ("pro", "site"), ("business", "staff")]
)
def test_a_site_stored_on_a_legacy_key_keeps_everything_it_had(legacy, current):
    """Every published site in production holds one of these strings.

    An unrecognised key resolves to None here and lands on the free floor, which
    means the rename alone — with no alias — would have returned the badge to
    every paying customer's site and revoked its custom domain, at deploy time,
    silently. The resolver has to answer identically for the old key and the new
    one.

    Breaks on: removing an entry from ``_LEGACY_SITE_TIER_ALIASES``.
    """
    old = _resolve(plan_tier=legacy, subscription_status="active")
    new = _resolve(plan_tier=current, subscription_status="active")

    assert old.plan_tier == current, "the answer must report the tier's CURRENT name"
    assert old.badge_required == new.badge_required
    assert old.custom_domain == new.custom_domain
    assert old.max_domained_sites == new.max_domained_sites
    assert old.concierge_entitled == new.concierge_entitled


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
