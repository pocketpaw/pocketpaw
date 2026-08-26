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
# NOT covered, deliberately: the badge stamper
# (``sites.service._stamp_free_badge``) reads NEITHER flag today and still reads
# neither. It badges a free site regardless of billing posture, which
# ``auth.site_keys.concierge_available`` already documents as a known inconsistency
# in the per-site family. Wiring it to this function would strip the attribution
# badge from every OSS / self-host site the moment this shipped, which is a
# product decision, not a refactor. Named here so the omission is visibly a choice.

# Updated 2026-08-26 (feat/concierge-conversation-quota): added
# ``concierge_conversation_quota_exceeded`` — the "200 conversations a month"
# allowance on the ``staff`` tier stops being a catalog claim and starts being a
# gate. It lives beside ``sites_enforced`` because it is the same family (a
# per-site billing seam) and because it needs that function; the module comment
# above already explains why ``billing`` rather than ``sites`` is the home.
#
# It is ASYNC and touches a store, which nothing else in the per-site entitlement
# path does. That is why it is not in ``entitlements.service``: the resolver there
# is deliberately pure so every branch runs without a database, and a quota is a
# question about history, not about a plan.

from __future__ import annotations

from datetime import datetime
from typing import Any


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


def _month_start() -> datetime:
    """Midnight on the 1st of the current month, on the clock the store writes.

    NAIVE LOCAL, deliberately, and it must stay that way. ``created_at`` on a
    conversation row is written with ``datetime.now().isoformat()`` — no timezone,
    local clock — and the count compares ISO strings. A UTC or aware boundary
    renders with an offset suffix that sorts after every stored value, so the
    count would come back 0 and the quota would never fire. Same clock, same shape.
    """
    now = datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def concierge_conversation_quota_exceeded(
    site: Any,
    *,
    widget_id: str,
    workspace_id: str,
    store: Any | None = None,
) -> bool:
    """Would STARTING another concierge conversation exceed this site's month?

    Asked only when a visitor's turn would begin a NEW conversation. A thread
    already in progress is never cut off part-way through: it was counted on the
    turn that started it, and refusing its next message would strand a visitor
    mid-sentence for a number they cannot see.

    Returns False (allow) in four cases, each for its own reason:

      * Billing is not enforced. OSS and self-host have no paywall, and this
        returns before it reads anything.
      * The site's tier is unresolvable, or is an org flat. The entitlement gate
        upstream has already decided whether a concierge is served at all; this
        function's job is only the ceiling.
      * The tier's allowance is 0. That is NOT "no conversations" — 0 means the
        tier sells no allowance, and reading it as a ceiling would refuse every
        conversation on a tier that is meant to be metered from the first one.
        ``agency`` is the tier designed that way, at its pooled rate; note it
        cannot actually reach this line today, because it is an ORG flat and
        ``site_scoped_tier`` refuses it above. The branch is what keeps a future
        metered per-site rung from being silently capped at zero.
      * The store read fails. A bookkeeping error must not silence a paying
        customer's concierge, so the failure direction is to serve.

    Fail-OPEN on error is the opposite of the entitlement gate's posture, and
    deliberately so: that gate answers "has this been paid for", where the safe
    error is refuse. This one answers "has a paid allowance been used up", where
    the safe error is allow — the alternative charges a customer for a tier and
    then withholds it because a count did not load.
    """
    if not sites_enforced():
        return False

    from pocketpaw_ee.cloud.billing import site_plans

    tier = site_plans.site_scoped_tier(getattr(site, "plan_tier", None))
    if tier is None:
        return False
    allowance = tier.conversation_allowance
    if allowance <= 0:
        return False

    if store is None:
        from pocketpaw_ee.api import get_paw_bar_store

        store = get_paw_bar_store()
    try:
        used = await store.count_conversations_started_since(
            widget_id, _month_start(), workspace_id
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "billing.concierge_quota: could not count conversations for widget %s — "
            "serving the concierge rather than refusing on a bookkeeping error",
            widget_id,
            exc_info=True,
        )
        return False
    return used >= allowance


__all__ = ["concierge_conversation_quota_exceeded", "sites_enforced"]
