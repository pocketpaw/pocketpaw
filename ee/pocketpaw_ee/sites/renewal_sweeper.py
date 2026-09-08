# ee/pocketpaw_ee/sites/renewal_sweeper.py — the RENEWAL for site plans bought
# from the workspace credit wallet.
#
# Created 2026-09-05 (fix/sites-plan-credits). A paid site now bills against the
# workspace's own credit balance rather than a Dodo subscription, and a Dodo
# subscription is the thing that used to make a MONTHLY plan actually recur. With
# nothing in its place a customer would pay for month one and hold every paid
# capability forever — the mirror image of the bug this branch set out to fix,
# and a worse one, because it looks like everything is working.
#
# So this is the missing half of the credits rail rather than an optimisation:
# once a site's ``renewal_date`` passes, its next month is debited and the date
# steps forward. It runs on the same 5-minute heartbeat as the pending sweeper.
#
# LAPSING IS THE INTERESTING CASE and it is deliberately gentle. When the wallet
# cannot cover the month the site is marked ``cancelled`` — which drops it to the
# free floor for every entitlement, so the badge returns, the custom domain stops
# resolving and the concierge goes quiet — but THE SITE ITSELF IS NEVER TAKEN
# DOWN. That is the rule the pricing spec states plainly ("the SITE itself always
# stays up"), and it is also just correct: a customer whose card lapses should
# lose the paid extras, not have their public web presence deleted. Republishing
# after a top-up buys the tier again through the ordinary purchase path.
#
# It NEVER deletes, never redeploys and never touches a site on another rail: a
# site with a Dodo subscription id or an add-on cart line renews at the gateway,
# and debiting it here would charge the customer twice for one month. The rail
# check is the whole tenancy of this module.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta

from pocketpaw_ee.cloud._core.errors import InsufficientCredits
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

logger = logging.getLogger(__name__)

# Cap per tick so a large backlog cannot wedge the shared heartbeat. A workspace
# with more due sites than this simply finishes on the next tick five minutes
# later, which is invisible at a monthly cadence.
_SWEEP_BATCH_LIMIT = 200

# Set POCKETPAW_SITE_RENEWALS_ENABLED=0 to stop charging renewals without
# redeploying. An escape hatch rather than a feature flag: if renewals ever start
# double-charging, an operator needs to be able to stop the money moving in one
# environment variable, not in a release.
_ENABLED_VAR = "POCKETPAW_SITE_RENEWALS_ENABLED"


def _enabled() -> bool:
    """Is the credits renewal sweep allowed to charge?

    Read at sweep time rather than at import, so an operator can turn it off on a
    running process. Defaults ON — a renewal that silently never happens is the
    failure this module exists to prevent, so the safe default is to charge.
    """
    raw = (os.environ.get(_ENABLED_VAR) or "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


async def sweep_site_renewals(*, now: datetime | None = None) -> dict[str, int]:
    """Charge the next month for every credits-paid site whose renewal is due —
    and END the ones that asked to be ended.

    Returns a count of what happened: ``{"renewed": n, "lapsed": n, "failed": n,
    "not_live": n, "closed": n}``. The last two are the rows this deliberately
    does NOT charge: ``not_live`` is a site that was billed and never deployed
    (an operator problem, not another month's debit), and ``closed`` is a plan the
    customer cancelled mid-period — cancelling schedules the close for the end of
    the month already paid for, and this sweep is where that date arrives.

    Selection is deliberately narrow, and each clause keeps money from moving
    where it should not:

      * ``billing_rail == "credits"`` — the ONLY rail this may charge. A site on
        a Dodo subscription or an add-on cart line renews at the gateway; a debit
        here would be the customer's second payment for one month.
      * ``subscription_status == "active"`` — a cancelled or pending site is not
        currently paying for anything, so there is nothing to renew.
      * ``renewal_date <= now`` — due. A site with no renewal date at all is
        skipped rather than charged: an absent date is an unknown, and guessing
        one bills somebody on a day nobody chose.

    Each site is charged in its own try, so one failure cannot stop the rest of
    the batch. A gateway-shaped error (anything that is not
    ``InsufficientCredits``) leaves the site untouched and due, and the next tick
    retries it — the debit is idempotent per (site, tier, renewal day), so a
    retry after a partial failure cannot double-charge.
    """
    if not _enabled():
        return {"renewed": 0, "lapsed": 0, "failed": 0, "not_live": 0, "closed": 0}

    from pocketpaw_ee.cloud.billing import service as billing_service
    from pocketpaw_ee.cloud.billing import site_plans

    at = now or datetime.now(UTC)
    due = (
        await _SiteDoc.find(
            {
                "billing_rail": billing_service.CREDITS_BILLING_RAIL,
                "subscription_status": "active",
                "renewal_date": {"$ne": None, "$lte": at},
            }
        )
        .limit(_SWEEP_BATCH_LIMIT)
        .to_list()
    )
    counts = {"renewed": 0, "lapsed": 0, "failed": 0, "not_live": 0, "closed": 0}
    if not due:
        return counts

    for doc in due:
        site_id = str(doc.id)

        # A CLOSE THE CUSTOMER ALREADY ASKED FOR. Dropping a paying site to the
        # free floor mid-period does not end it on the spot — the month was bought
        # and the site keeps its tier and its capabilities until it runs out — it
        # sets ``plan_cancels_at_period_end``. This is where it runs out.
        #
        # Checked FIRST, ahead of the not-deployed skip below, because that skip
        # leaves a row due forever on purpose: a site scheduled to close that never
        # deployed would otherwise be re-examined every tick for good, never
        # charged and never closed.
        #
        # The row is left in the same shape an immediate downgrade produces — free
        # tier, no subscription, no renewal, nothing pre-paid — so nothing
        # downstream has to learn a second way of reading "this site is free now".
        if getattr(doc, "plan_cancels_at_period_end", False):
            doc.plan_tier = site_plans.BASE_SITE_PLAN_KEY
            doc.subscription_status = "none"
            doc.renewal_date = None
            doc.period_paid_usd = 0
            doc.plan_cancels_at_period_end = False
            await doc.save()
            counts["closed"] += 1
            logger.info(
                "sites.renewal_sweeper: site %s (workspace=%s) reached the end of "
                "the period it paid for and closed as requested — it stays live on "
                "the free floor and was NOT charged",
                site_id,
                doc.workspace,
            )
            continue

        # NEVER CHARGE FOR A SITE THAT IS NOT UP. A row can be "active" and not
        # deployed: ``activate_site`` marks the subscription active BEFORE running
        # the deploy — deliberately, because the badge and concierge stampers read
        # the status mid-deploy — so a deploy that raises leaves exactly this
        # shape. The webhook that used to retry it is gone with the gateway, and
        # the pending sweeper only looks for ``subscription_status == "pending"``,
        # so nothing else sees these rows at all.
        #
        # Without this the customer is billed a month, every month, for a site
        # that 404s. Skipping leaves ``renewal_date`` in the past, so the sweep
        # re-examines it every tick and charges the moment it is genuinely live —
        # a republish redeploys it and costs nothing, because the site is already
        # inside a period it has paid for.
        #
        # A dynamic site mid-provision has this shape too, legitimately and for
        # minutes. Deferring its renewal by one five-minute tick is the correct
        # answer there as well.
        if not getattr(doc, "deployed", False):
            counts["not_live"] += 1
            logger.warning(
                "sites.renewal_sweeper: site %s (workspace=%s) is due but is NOT "
                "DEPLOYED — skipping the charge and leaving it due. A paying site "
                "that never deployed needs an operator, not another month's debit.",
                site_id,
                doc.workspace,
            )
            continue

        tier = site_plans.site_scoped_tier(getattr(doc, "plan_tier", None))
        if tier is None or tier.monthly_price_usd <= 0:
            # The site is on the free floor, or on a key the catalog no longer
            # resolves. Either way there is nothing to charge, and stamping a new
            # renewal date would keep re-selecting it every tick forever.
            doc.renewal_date = None
            # Nothing is being paid for any more, so nothing is pre-paid. Left
            # standing, this would discount the next real purchase by whatever the
            # site used to be on.
            doc.period_paid_usd = 0
            await doc.save()
            continue

        # THE PERIOD BEING BOUGHT IS THE DATE ON THE ROW, not today. It is what
        # makes the debit idempotent: a sweep that crashes after the charge and
        # before the save re-reads the same due date next tick, computes the same
        # idempotency key, and no-ops instead of charging a second time.
        period = doc.renewal_date or at
        try:
            await billing_service.charge_site_plan_credits(
                workspace_id=doc.workspace,
                site_id=site_id,
                tier_key=tier.key,
                # A RENEWAL BUYS THE WHOLE MONTH. The publish path is the one that
                # charges a difference, and only when a tier changes mid-period.
                amount_usd=tier.monthly_price_usd,
                period_start=period,
                member_id=None,
            )
        except InsufficientCredits:
            # LAPSED. Drop to the free floor and stop trying; the site stays up.
            doc.subscription_status = "cancelled"
            doc.renewal_date = None
            # The period the wallet could not buy was never paid for, so it must
            # not discount whatever the customer buys after topping up.
            doc.period_paid_usd = 0
            await doc.save()
            counts["lapsed"] += 1
            logger.warning(
                "sites.renewal_sweeper: site %s (workspace=%s tier=%s) could not renew — "
                "the wallet is short $%s. The site STAYS LIVE and drops to the free "
                "floor; republishing after a top-up buys the tier again.",
                site_id,
                doc.workspace,
                tier.key,
                tier.monthly_price_usd,
            )
            continue
        except Exception:
            counts["failed"] += 1
            logger.exception(
                "sites.renewal_sweeper: site %s (workspace=%s tier=%s) failed to renew; "
                "left due and will be retried on the next tick",
                site_id,
                doc.workspace,
                tier.key,
            )
            continue

        # Step from the DUE date, not from now. Stepping from now would let each
        # sweep's few seconds of lateness accumulate, walking every customer's
        # billing day slowly forward through the calendar.
        doc.renewal_date = period + relativedelta(months=1)
        # A NEW PERIOD RESETS WHAT IT HAS BEEN PAID FOR. The high-water mark is
        # per-period by definition, so carrying last month's across would let a
        # customer who upgraded in March take the April upgrade for free forever.
        # Set to the tier just charged, which is exactly what this month bought.
        doc.period_paid_usd = int(tier.monthly_price_usd)
        await doc.save()
        counts["renewed"] += 1

    # ``closed`` is deliberately NOT in the warning condition: a site reaching a
    # cancellation the customer asked for is the system working, and warning on it
    # would train the operator to ignore the line that also carries ``failed``.
    if counts["lapsed"] or counts["failed"] or counts["not_live"]:
        logger.warning(
            "sites.renewal_sweeper: renewed=%d lapsed=%d failed=%d not_live=%d closed=%d",
            counts["renewed"],
            counts["lapsed"],
            counts["failed"],
            counts["not_live"],
            counts["closed"],
        )
    else:
        logger.info(
            "sites.renewal_sweeper: renewed=%d closed=%d",
            counts["renewed"],
            counts["closed"],
        )
    return counts
