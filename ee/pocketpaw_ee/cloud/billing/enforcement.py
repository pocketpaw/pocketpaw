# ee/pocketpaw_ee/cloud/billing/enforcement.py — one function answering "do the
# PER-SITE billing seams enforce right now".
#
# Created 2026-08-21 (feat/sites-billing-flag, PW-2). Until now every sites seam
# read ``billing_enforced`` directly, which is the workspace-wide switch: turning
# it on to start charging for custom domains also starts 402ing chat runs, seat
# invites, pocket creates, connector enables and uploads. Those are unrelated
# decisions on unrelated timelines, and needing them to be the same one is why the
# sites paywall could not be switched on at all.
#
# ``sites_billing_enforced`` is the second switch. The condition is the OR of the
# two, never a replacement: a deployment already setting the global flag keeps
# behaving exactly as ``configuration-reference.mdx`` documents it, so this change
# is additive for every existing tenant.
#
# It lives in ONE function rather than being written at each seam because there
# are five of them, and a compound condition copied five times is a condition that
# will eventually read differently in one place. ``billing`` rather than ``sites``
# is the home so ``cloud.auth.site_keys`` can reach it without importing the sites
# package.
#
# Updated 2026-08-21 (fix/sites-concierge-flag): ``sites_enforced()`` covers the
# DOMAIN caps and nothing else. It used to cover the visitor concierge too, and
# on the day the flag was first enabled in production that took the concierge off
# every site at once — see ``concierge_enforced()`` below for why that could never
# have been anything but an outage. The two questions are now two functions.
#
# NOT covered, deliberately: the badge stamper
# (``sites.service._stamp_free_badge``) reads NEITHER flag today and still reads
# neither. It badges a free site regardless of billing posture, which
# ``auth.site_keys.concierge_available`` already documents as a known inconsistency
# in the per-site family. Wiring it to this function would strip the attribution
# badge from every OSS / self-host site the moment this shipped, which is a
# product decision, not a refactor. Named here so the omission is visibly a choice.

from __future__ import annotations


def sites_enforced() -> bool:
    """Are the per-site billing seams live?

    True when EITHER the workspace-wide ``billing_enforced`` or the sites-only
    ``sites_billing_enforced`` is set. Both default False, so OSS / self-host
    sees no paywall and every seam returns before it reads a database.

    ``getattr`` with a default rather than plain attribute access: settings are
    routinely stubbed in tests with a namespace carrying only the fields under
    test, and a seam that raises AttributeError because a stub predates a new flag
    fails for a reason that has nothing to do with billing. Absent reads as off,
    which is also the correct fail-open direction for a paywall.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    return bool(
        getattr(settings, "billing_enforced", False)
        or getattr(settings, "sites_billing_enforced", False)
    )


def concierge_enforced() -> bool:
    """Is the per-site VISITOR CONCIERGE entitlement gate live?

    Reads ``sites_concierge_enforced`` and NOTHING else. Not ``billing_enforced``,
    not ``sites_billing_enforced`` — and the fact that it is not an OR is the whole
    content of this function.

    Both of those are paywalls that convert: hit the pocket cap and you can buy
    more pockets, hit the domain cap and you can buy another site plan. The
    concierge gate converts nobody. ``SitePlanTier.sells_concierge`` is
    ``key != BASE_SITE_PLAN_KEY`` and every site in production is on the floor;
    even a site that were not would need ``subscription_status == "active"``,
    which is only ever written by ``activate_site`` off a Dodo webhook, which
    needs a product id from ``POCKETPAW_DODO_SITE_PRODUCTS`` — configured in no
    deployment file anywhere. So switching this on removes the concierge from
    every site and offers no customer any way to get it back. That is an outage
    wearing a paywall's clothes, and it is what happened on 2026-08-21 when
    enabling ``sites_billing_enforced`` for the domain caps silently did exactly
    this.

    The gate itself is correct and stays reachable. It wants turning on the day
    the ``staff`` tier exists and can be bought, and not one deploy earlier.
    """
    from pocketpaw.config import get_settings

    return bool(getattr(get_settings(), "sites_concierge_enforced", False))


__all__ = ["concierge_enforced", "sites_enforced"]
