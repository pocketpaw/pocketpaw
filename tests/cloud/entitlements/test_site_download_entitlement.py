# tests/cloud/entitlements/test_site_download_entitlement.py — proves the
# PER-SITE capability ``SiteEntitlements.project_download`` ("may this site's
# project be downloaded") resolves, fails closed, and survives the site-plan
# rekey.
#
# Four properties, in the order a reviewer should check them:
#   (a) both paid rungs resolve it True and ``free`` resolves it False — and the
#       floor stays False even when a subscription on it reads "active", which is
#       what proves the allow-set is consulted rather than the branch alone;
#   (b) a paid tier whose subscription is not paying resolves False. That is the
#       bug this resolver exists to prevent: cancellation never resets
#       ``plan_tier``, so reading the tier alone hands a customer's project to
#       somebody who stopped paying;
#   (c) a key the catalog cannot resolve — the retired ``studio``/``agency``, a
#       typo, an absent value — lands on the free floor and resolves False;
#   (d) a LEGACY key resolves to the tier carrying the same capabilities, so a
#       site whose document still says ``pro`` keeps what it has always paid for.
#
# DETERMINISTIC AND DB-FREE. ``resolve_site_entitlements`` is pure and synchronous
# — it takes the site's billing fields because ``entitlements`` may not import
# ``models.site`` (EE cloud rule 2) — so every branch here is exercised by calling
# it, with no database, no clock and no network.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.entitlements import service as entitlements
from pocketpaw_ee.cloud.entitlements.service import resolve_site_entitlements

_SITE = "6512c1f0e4b0a1b2c3d4e5f6"
_WS = "ws_site_download_test"

# The per-site rungs that sell the download. ``free`` is the floor and is absent.
PAID_TIERS = ["site", "staff"]

# Keys that resolve to NO tier: the two org flats retired on 2026-09-06 (still
# present in stored documents, because nothing rewrites a document on read), a
# workspace plan key that is not a site plan at all, and a plain typo.
UNRESOLVABLE_TIERS = ["studio", "agency", "pro_max", "stafff"]

# Every inactive subscription state a stored document can hold. ``none`` is the
# one with teeth: a paid publish with no gateway product records the tier and
# never charges.
INACTIVE_STATUSES = ["cancelled", "none", "pending", "", None, "garbage"]


def _resolve(**ov):
    kw = {
        "site_id": _SITE,
        "workspace_id": _WS,
        "plan_tier": "site",
        "subscription_status": "active",
        "concierge_enabled": False,
    }
    kw.update(ov)
    return resolve_site_entitlements(**kw)


# ---------------------------------------------------------------------------
# (a) The grant itself — paid yes, free no.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", PAID_TIERS)
def test_a_paying_site_may_download_its_project(tier):
    """Both per-site rungs carry the download once the subscription is paying."""
    ent = _resolve(plan_tier=tier, subscription_status="active")

    assert ent.plan_tier == tier
    assert ent.subscription_active is True
    assert ent.project_download is True


def test_a_free_site_may_not_download_its_project():
    """The floor is withheld — and by falling through the default, not by a denial."""
    ent = _resolve(plan_tier="free", subscription_status="none")

    assert ent.plan_tier == "free"
    assert ent.project_download is False


def test_a_free_site_with_an_active_subscription_is_still_withheld():
    """The one case that separates "the allow-set is read" from "the branch is read".

    ``free`` is a real catalog tier, so a site sitting on the floor with a status
    of "active" enters the paid branch exactly as ``site`` does. Everything in that
    branch is therefore reached for a free site, and only the allow-set keeps the
    download from being granted there. A resolver that stopped consulting the set —
    granting to anything that reached the branch — passes every other test in this
    file and fails this one.
    """
    ent = _resolve(plan_tier="free", subscription_status="active")

    assert ent.subscription_active is True
    assert ent.project_download is False


def test_the_free_floor_is_not_in_the_granting_set():
    """The floor is absent from the allow-set, which is what makes it withheld.

    Asserted on the set as well as on the resolved value because those are two
    different bugs with the same symptom today: a resolver that stopped reading the
    set would still answer False for free, and only this catches a later edit that
    adds the floor to it.
    """
    assert "free" not in entitlements._PROJECT_DOWNLOAD_PLANS
    assert set(entitlements._PROJECT_DOWNLOAD_PLANS) == set(PAID_TIERS)


# ---------------------------------------------------------------------------
# (b) The hole this resolver exists to close — a paid TIER, no paying
#     SUBSCRIPTION.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", PAID_TIERS)
@pytest.mark.parametrize("status", INACTIVE_STATUSES)
def test_a_paid_tier_without_an_active_subscription_may_not_download(tier, status):
    """Cancellation leaves ``plan_tier`` on the paid key and nothing resets it, so a
    resolver reading the tier alone keeps handing over the project forever. The
    ``none`` row is the same hole from the other side: a paid publish with no
    gateway product records the tier with no charge at all."""
    ent = _resolve(plan_tier=tier, subscription_status=status)

    assert ent.subscription_active is False
    assert ent.project_download is False


def test_losing_the_subscription_costs_the_download_but_not_the_floor():
    """A lapsed site is worse off than a paying one and no worse off than a free
    one. Pinned together because they are the same branch: the paid grants fall
    away while ``max_domained_sites`` stays on free's floor of 1."""
    ent = _resolve(plan_tier="staff", subscription_status="cancelled")

    assert ent.project_download is False
    assert ent.max_domained_sites == 1


# ---------------------------------------------------------------------------
# (c) Fail closed on a key the catalog cannot resolve.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", UNRESOLVABLE_TIERS)
def test_a_retired_or_unknown_tier_may_not_download(tier):
    """``studio`` and ``agency`` are the load-bearing rows: both were retired on
    2026-09-06 and both still sit in stored documents. ``site_scoped_tier`` returns
    None for them, which lands on the free floor — so a resolver that matched the
    RAW ``plan_tier`` string against the allow-set, or that read "anything not
    free", would hand a retired key the download."""
    ent = _resolve(plan_tier=tier, subscription_status="active")

    assert ent.plan_tier == "free"
    assert ent.project_download is False


def test_a_site_with_no_tier_at_all_may_not_download():
    """``create_draft_site`` sets neither billing field, so an unpublished site
    arrives here as a pair of Nones."""
    ent = _resolve(plan_tier=None, subscription_status=None)

    assert ent.plan_tier == "free"
    assert ent.project_download is False


# ---------------------------------------------------------------------------
# (d) The rekey — a legacy key keeps what it paid for.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "current"),
    [("basic", "free"), ("pro", "site"), ("business", "staff")],
)
def test_a_legacy_key_resolves_to_the_tier_that_carries_the_same_capabilities(legacy, current):
    """``Site.plan_tier`` holds the pre-2026-08-22 keys in production and nothing
    rewrites a document on read, so ``basic``/``pro``/``business`` must resolve
    forever. The grant reads the RESOLVED tier's key, which is why the allow-set
    holds canonical names only: ``pro`` is a ``site`` and is entitled to exactly
    what ``site`` is. Matching the raw string would silently demote every site that
    has not been migrated."""
    ent = _resolve(plan_tier=legacy, subscription_status="active")

    assert ent.plan_tier == current
    assert ent.project_download is (current in PAID_TIERS)


def test_a_legacy_key_without_a_paying_subscription_is_still_withheld():
    """The alias resolves the tier; it does not excuse the payment."""
    ent = _resolve(plan_tier="business", subscription_status="cancelled")

    assert ent.plan_tier == "staff"
    assert ent.project_download is False


# ---------------------------------------------------------------------------
# The default, and the neighbours it must not disturb.
# ---------------------------------------------------------------------------


def test_the_field_defaults_to_withheld_when_a_construction_omits_it():
    """``SiteEntitlements`` is built by name, and the one test double in the tree
    that builds it does not know this field exists. The default decides which way
    that omission fails, and it must fail closed."""
    from pocketpaw_ee.cloud.entitlements.domain import SiteEntitlements

    ent = SiteEntitlements(
        site_id=_SITE,
        workspace_id=_WS,
        plan_tier="staff",
        subscription_active=True,
        badge_required=False,
        custom_domain=True,
        max_domained_sites=None,
        analytics=True,
        concierge_enabled=True,
        concierge_entitled=True,
    )

    assert ent.project_download is False


def test_the_download_grant_leaves_the_other_paid_capabilities_alone():
    """A guard on the slice itself: the new field rides in the same branch as the
    badge and the concierge, and this pins that it did not disturb either."""
    paying = _resolve(plan_tier="staff", subscription_status="active", concierge_enabled=True)

    assert paying.project_download is True
    assert paying.badge_required is False
    assert paying.concierge_entitled is True
    assert paying.analytics is True

    floor = _resolve(plan_tier="free", subscription_status="none", concierge_enabled=True)

    assert floor.project_download is False
    assert floor.badge_required is True
    assert floor.concierge_entitled is False
    assert floor.analytics is False
