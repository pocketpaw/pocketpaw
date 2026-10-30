# tests/cloud/billing/test_site_plan_domain_quota.py — the catalog half of "free
# includes a custom domain".
#
# The rule: pricing is per site, so every free site may carry its own custom
# domain (apex + ``www``). The seam that enforces it is tested in
# tests/cloud/sites/test_custom_domain_cap.py; what is pinned HERE is the
# declaration it reads — the ladder's shape, and the two ways a careless edit to it
# goes wrong quietly.
#
# 1. ONE DOMAIN IS TWO HOSTNAMES. ``max_domained_sites`` is the per-site grant,
#    and the hostname ceiling that bounds it must leave room for apex + www.
# 2. MISSING IS NOT UNCAPPED. ``None`` in the map means uncapped, so an absent key
#    must resolve to 0 rather than to None. ``.get(key)`` and ``.get(key, 0)`` look
#    equally reasonable and mean opposite things.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402


def test_the_floor_includes_one_domained_site():
    """Every free site may carry one custom domain, at the catalog."""
    floor = site_plans.get_site_plan(site_plans.BASE_SITE_PLAN_KEY)

    assert floor is not None
    assert floor.max_domained_sites == 1


def test_the_floor_still_resells_no_cloudflare_features():
    """A custom domain on free is OUR allowance, not a resold Cloudflare feature.

    ``cloudflare_features`` drives what BC-10 provisions on the custom hostname, and
    the floor resells nothing — that is what free means. The entitlement question
    moved off this set entirely, which is the point: it now says only what its name
    says.
    """
    floor = site_plans.get_site_plan(site_plans.BASE_SITE_PLAN_KEY)

    assert floor is not None
    assert floor.cloudflare_features == frozenset()


def test_every_paid_tier_is_uncapped():
    """What a paid tier sells is the absence of a ceiling, not the capability."""
    paid = [t for t in site_plans.list_site_plans() if t.key != site_plans.BASE_SITE_PLAN_KEY]

    assert paid, "catalog has no tier above the floor — the ladder changed"
    assert all(t.max_domained_sites is None for t in paid)


def test_an_unknown_key_gets_no_domains_at_all():
    """Fail-closed, and the branch that is one character from being wrong.

    ``None`` means uncapped in this map, so a missing key must come back as 0.
    ``_build`` is only reached through ``get_site_plan`` with known keys, which is
    exactly why the defensive default needs its own test — nothing else exercises
    it, and getting it wrong hands an unknown tier the most generous answer.
    """
    assert site_plans._build("no-such-tier").max_domained_sites == 0


def test_the_ladder_never_narrows_as_it_climbs():
    """A more expensive tier may not grant LESS domain allowance than a cheaper one.

    Cheap to assert, and it catches the rekey mistake nobody notices in review: the
    five tier-keyed maps in this module are edited together, and one of them being
    left in the old order produces a ladder that silently sells less for more.
    """
    ladder = site_plans.list_site_plans()
    # None sorts as "uncapped" — treat it as larger than any integer.
    values = [
        float("inf") if t.max_domained_sites is None else t.max_domained_sites for t in ladder
    ]

    assert values == sorted(values), f"domain allowance narrows going up the ladder: {values}"


def test_the_hostname_companion_cap_leaves_room_for_apex_and_www():
    """One free domain is only humane if the site may hold both spellings.

    A cap of 1 here would refuse ``www`` on every free site — so the floor is 2,
    not 1.
    """
    assert site_plans.free_max_hostnames_per_site() >= 2
