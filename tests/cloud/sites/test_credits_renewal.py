# tests/cloud/sites/test_credits_renewal.py — the RENEWAL half of the credits
# rail: a monthly site plan bought from the wallet has to charge every month.
#
# WHY THIS EXISTS AT ALL. A Dodo subscription is what used to make a MONTHLY site
# plan recur; a wallet-paid site does not have one. Ship the purchase without
# this and the customer pays for month one and keeps badge removal, the custom
# domain and the concierge for good — which is the mirror image of the bug the
# credits rail was written to fix, and worse, because everything looks healthy.
#
# The three that are worth the most:
#
#   * ``test_a_due_site_is_charged_and_stepped_forward`` — the feature.
#   * ``test_a_site_on_another_rail_is_never_charged_here`` — the double charge.
#     A site on a Dodo subscription or an add-on cart line renews at the gateway;
#     debiting it here takes a second payment for one month, from a wallet the
#     customer topped up for something else.
#   * ``test_a_short_wallet_lapses_the_plan_but_leaves_the_site_up`` — the rule
#     the pricing spec states outright: the paid extras go, the site never does.
#     Taking a customer's public web presence down over a lapsed balance is not a
#     downgrade, it is data loss with a billing excuse.
#
# Created 2026-09-05 (fix/sites-plan-credits): new test module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

pytestmark = pytest.mark.anyio


async def _fund(workspace_id: str, credits: int) -> None:
    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}",
    )


async def _seed_site(
    *,
    workspace_id: str,
    tier: str = "site",
    rail: str = "credits",
    status: str = "active",
    renewal_date: datetime | None,
    subscription_id: str | None = None,
    deployed: bool = True,
    cancels_at_period_end: bool = False,
) -> Site:
    doc = Site(
        workspace=workspace_id,
        pocket_id=f"pk_{workspace_id}",
        owner="u1",
        name="My Site",
        deployed=deployed,
        url="http://local/site/",
        plan_tier=tier,
        subscription_status=status,
        billing_rail=rail,
        subscription_id=subscription_id,
        renewal_date=renewal_date,
        plan_cancels_at_period_end=cancels_at_period_end,
    )
    await doc.insert()
    return doc


def _price(key: str) -> int:
    tier = site_plans.get_site_plan(key)
    assert tier is not None
    return tier.monthly_price_usd * 100


def _yesterday() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


def _aware(value: datetime) -> datetime:
    """Re-attach UTC to a datetime read back from the database.

    BSON has no timezone, so a stamp written as aware comes back NAIVE and
    comparing it against ``datetime.now(UTC)`` raises rather than failing. The
    sweeper itself is unaffected — its due-date comparison is the Mongo ``$lte``,
    which is done server-side — so this belongs in the test, not in the code."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #


async def test_a_due_site_is_charged_and_stepped_forward(mongo_db):  # noqa: ARG001
    """The feature. A month passes, the wallet pays for the next one."""
    ws = "ws_renew"
    await _fund(ws, 5000)
    due = _yesterday()
    doc = await _seed_site(workspace_id=ws, renewal_date=due)

    counts = await sweep_site_renewals()

    assert counts["renewed"] == 1
    assert await credits_service.balance(ws) == 5000 - _price("site")
    refreshed = await Site.get(str(doc.id))
    assert refreshed.subscription_status == "active"
    assert refreshed.renewal_date is not None
    assert _aware(refreshed.renewal_date) > datetime.now(UTC), "the next charge is a month out"


async def test_a_site_that_is_not_due_yet_is_left_alone(mongo_db):  # noqa: ARG001
    """Paying early is still paying twice."""
    ws = "ws_not_due"
    await _fund(ws, 5000)
    future = datetime.now(UTC) + timedelta(days=10)
    await _seed_site(workspace_id=ws, renewal_date=future)

    counts = await sweep_site_renewals()

    assert counts == {"renewed": 0, "lapsed": 0, "failed": 0, "not_live": 0, "closed": 0}
    assert await credits_service.balance(ws) == 5000


async def test_a_site_on_another_rail_is_never_charged_here(mongo_db):  # noqa: ARG001
    """THE DOUBLE CHARGE. A site billed by Dodo — as a per-site subscription or
    as an add-on line on the workspace subscription — is renewed BY Dodo. Taking
    its month out of the wallet as well charges the customer twice, out of a
    balance they topped up for something else entirely.

    Both non-credits shapes are seeded, because the add-on one is the dangerous
    half: it looks exactly like a credits site on every field except the rail."""
    ws = "ws_other_rails"
    await _fund(ws, 5000)
    due = _yesterday()
    await _seed_site(workspace_id=ws, rail="addon", renewal_date=due)
    await _seed_site(
        workspace_id=ws, rail="subscription", subscription_id="sub_x", renewal_date=due
    )
    # And a row written before ``billing_rail`` existed, which is every paid site
    # in production today. Empty must read as "not credits" — the safe direction.
    await _seed_site(workspace_id=ws, rail="", renewal_date=due)

    counts = await sweep_site_renewals()

    assert counts["renewed"] == 0
    assert await credits_service.balance(ws) == 5000, "Dodo bills these, not the wallet"


async def test_a_cancelled_site_is_not_renewed(mongo_db):  # noqa: ARG001
    """Cancelled means it stopped paying. Charging it again would resurrect a
    plan the customer ended."""
    ws = "ws_cancelled"
    await _fund(ws, 5000)
    await _seed_site(workspace_id=ws, status="cancelled", renewal_date=_yesterday())

    counts = await sweep_site_renewals()

    assert counts["renewed"] == 0
    assert await credits_service.balance(ws) == 5000


async def test_a_short_wallet_lapses_the_plan_but_leaves_the_site_up(mongo_db):  # noqa: ARG001
    """THE RULE THE PRICING SPEC STATES OUTRIGHT: the paid extras go, the site
    stays up.

    Dropping to the free floor returns the attribution badge, stops the custom
    domain resolving and silences the concierge. Taking the page down instead
    would be data loss dressed as a downgrade — the customer's public web
    presence disappearing over a balance they can top up in a minute."""
    ws = "ws_short"
    await _fund(ws, 100)  # less than a month of any paid rung
    doc = await _seed_site(workspace_id=ws, renewal_date=_yesterday())

    counts = await sweep_site_renewals()

    assert counts["lapsed"] == 1
    assert await credits_service.balance(ws) == 100, "a refused debit moves no money"
    refreshed = await Site.get(str(doc.id))
    assert refreshed.subscription_status == "cancelled", "the paid capabilities go"
    assert refreshed.deployed is True, "and the site itself stays live"
    assert refreshed.renewal_date is None, "it must not be retried every five minutes"


async def test_a_free_site_is_dropped_from_the_due_set(mongo_db):  # noqa: ARG001
    """A site on the floor has nothing to charge. Its renewal date is cleared
    rather than stepped, so it stops being re-selected on every single tick — a
    row that can never be charged and is never dropped is an infinite loop that
    only shows up as log noise."""
    ws = "ws_free"
    await _fund(ws, 5000)
    doc = await _seed_site(workspace_id=ws, tier="free", renewal_date=_yesterday())

    await sweep_site_renewals()

    assert await credits_service.balance(ws) == 5000
    refreshed = await Site.get(str(doc.id))
    assert refreshed.renewal_date is None


async def test_month_two_is_a_separate_charge_from_month_one(mongo_db):  # noqa: ARG001
    """THE OTHER HALF OF THE IDEMPOTENCY KEY, and the direction that fails silently.

    The key has to be identical across calls that mean the same month and
    DIFFERENT across calls that mean different months. Its sibling test below
    covers the first. This covers the second: drop the period from the key and
    every renewal after the first collides with the original purchase and no-ops,
    so the customer pays once and the sweep reports a healthy "renewed" forever.

    Two sweeps a month apart, and the wallet must be down two months.

    The site starts 40 days overdue, so the first sweep's step forward — one month
    from the DUE date — lands about ten days ago, still in the past. The second
    sweep therefore finds it due on its own, with a genuinely different period, and
    no test-side clock rewinding is involved. Setting it back to the same day by
    hand would collide on the key for the right reason and pass while proving
    nothing."""
    ws = "ws_two_months"
    await _fund(ws, 5000)
    await _seed_site(workspace_id=ws, renewal_date=datetime.now(UTC) - timedelta(days=40))

    assert (await sweep_site_renewals())["renewed"] == 1
    assert await credits_service.balance(ws) == 5000 - _price("site")

    # The date the first sweep wrote is itself in the past, so the next tick is
    # month two arriving on schedule.
    assert (await sweep_site_renewals())["renewed"] == 1

    assert await credits_service.balance(ws) == 5000 - 2 * _price("site"), (
        "month two was free — the debit key does not distinguish one month from "
        "the next, so every renewal replays the original purchase"
    )


async def test_a_replayed_sweep_does_not_charge_the_month_twice(mongo_db):  # noqa: ARG001
    """The crash window. A sweep that charges and then dies before saving the new
    date re-reads the same due row next tick.

    The debit's idempotency key is built from the DUE DATE, not from now, so the
    replay computes the same key and no-ops. Simulated by rewinding the row to
    its original due date after a successful sweep — which is exactly the state a
    crash between the charge and the save leaves behind."""
    ws = "ws_replay"
    await _fund(ws, 5000)
    due = _yesterday()
    doc = await _seed_site(workspace_id=ws, renewal_date=due)

    await sweep_site_renewals()
    after_first = await credits_service.balance(ws)
    assert after_first == 5000 - _price("site")

    # Rewind to the pre-save state and sweep again.
    refreshed = await Site.get(str(doc.id))
    refreshed.renewal_date = due
    await refreshed.save()

    await sweep_site_renewals()

    assert await credits_service.balance(ws) == after_first, (
        "the same month was charged twice — the idempotency key is not anchored "
        "on the period being bought"
    )


async def test_the_kill_switch_stops_every_charge(mongo_db, monkeypatch):  # noqa: ARG001
    """An operator has to be able to stop money moving with an environment
    variable rather than a release."""
    ws = "ws_killswitch"
    await _fund(ws, 5000)
    await _seed_site(workspace_id=ws, renewal_date=_yesterday())
    monkeypatch.setenv("POCKETPAW_SITE_RENEWALS_ENABLED", "0")

    counts = await sweep_site_renewals()

    assert counts == {"renewed": 0, "lapsed": 0, "failed": 0, "not_live": 0, "closed": 0}
    assert await credits_service.balance(ws) == 5000


async def test_renewals_do_not_walk_the_billing_day_forward(mongo_db):  # noqa: ARG001
    """Stepping from NOW rather than from the due date would add each sweep's
    lateness to every cycle, sliding a customer's billing day through the
    calendar over a year. The new date is one month after the date that came
    due, whenever the sweep happened to run."""
    ws = "ws_drift"
    await _fund(ws, 5000)
    due = datetime.now(UTC) - timedelta(days=3)
    doc = await _seed_site(workspace_id=ws, renewal_date=due)

    await sweep_site_renewals()

    refreshed = await Site.get(str(doc.id))
    assert refreshed.renewal_date is not None
    # One month after the DUE date, so still three days behind "a month from now".
    assert _aware(refreshed.renewal_date) < datetime.now(UTC) + timedelta(days=29)


async def test_a_renewed_month_is_recorded_as_paid_for(mongo_db):  # noqa: ARG001
    """A renewal buys a month, so it has to say what that month bought.

    ``period_paid_usd`` is what a mid-period tier change subtracts against. Left
    at zero after a renewal, the next upgrade computes its difference from
    nothing and charges the new tier's whole month on top of the one the sweep
    just took."""
    ws = "ws_renew_mark"
    await _fund(ws, 5000)
    doc = await _seed_site(workspace_id=ws, tier="staff", renewal_date=_yesterday())

    await sweep_site_renewals()

    refreshed = await Site.get(str(doc.id))
    assert refreshed.period_paid_usd == 19


async def test_a_renewal_drops_a_stale_high_water_mark(mongo_db):  # noqa: ARG001
    """The mark has to FALL when the new month is cheaper than the last one.

    A site that bought staff and dropped to site mid-period carries a $19 mark
    for the rest of that period, which is correct — the customer paid for staff
    and owns it until the period ends. Carrying it past the renewal is not: the
    sweep charges $7 for the new month, so going back up to staff has to cost the
    $12 difference again rather than being free forever."""
    ws = "ws_renew_stale_mark"
    await _fund(ws, 5000)
    doc = await _seed_site(workspace_id=ws, tier="site", renewal_date=_yesterday())
    doc.period_paid_usd = 19
    await doc.save()

    await sweep_site_renewals()

    refreshed = await Site.get(str(doc.id))
    assert refreshed.period_paid_usd == 7


async def test_a_site_that_never_deployed_is_not_charged(mongo_db):  # noqa: ARG001
    """A PAYING SITE THAT IS NOT UP MUST NOT BE BILLED ANOTHER MONTH.

    ``activate_site`` marks the subscription active BEFORE it deploys — on
    purpose, because the badge and concierge stampers read the status mid-deploy
    — so a deploy that raises leaves the row active with ``deployed=False``. The
    gateway webhook that used to retry it is gone, and the pending sweeper only
    looks for "pending", so nothing else ever sees these rows.

    Charging one is billing a customer monthly for a page that 404s. The sweep
    skips it and leaves it due, so it renews the moment it is genuinely live."""
    ws = "ws_never_deployed"
    await _fund(ws, 5000)
    doc = await _seed_site(workspace_id=ws, renewal_date=_yesterday(), deployed=False)

    counts = await sweep_site_renewals()

    assert await credits_service.balance(ws) == 5000, "an undeployed site was billed"
    assert counts["not_live"] == 1
    assert counts["renewed"] == 0
    # Left DUE rather than pushed forward: the next tick re-examines it, and the
    # charge lands as soon as a republish gets the site up.
    refreshed = await Site.get(str(doc.id))
    assert refreshed.renewal_date is not None
    assert _aware(refreshed.renewal_date) < datetime.now(UTC)


async def test_a_scheduled_close_ends_the_plan_instead_of_charging(mongo_db):  # noqa: ARG001
    """The other half of cancelling.

    Dropping a paying site to the free floor does not end it on the spot — the
    month was bought, so the plan and its capabilities stand until the period runs
    out. This sweep is where it runs out, and the one thing it must not do on the
    way is take another month's money for a plan the customer has already
    cancelled.

    The row is left in exactly the shape a free site has, so nothing downstream
    needs a second way to read "this site is not paying any more"."""
    ws = "ws-close"
    await _fund(ws, 9000)
    doc = await _seed_site(
        workspace_id=ws, tier="staff", renewal_date=_yesterday(), cancels_at_period_end=True
    )

    counts = await sweep_site_renewals()

    assert counts["closed"] == 1
    assert counts["renewed"] == 0
    assert await credits_service.balance(ws) == 9000, "a cancelled plan is not renewed"

    fresh = await Site.get(doc.id)
    assert fresh.plan_tier == "free"
    assert fresh.subscription_status == "none"
    assert fresh.renewal_date is None
    assert fresh.period_paid_usd == 0
    assert fresh.plan_cancels_at_period_end is False
    assert fresh.deployed is True, "closing a plan must not take the site down"


async def test_a_scheduled_close_beats_the_not_deployed_skip(mongo_db):  # noqa: ARG001
    """Order matters between the two guards, and only one order terminates.

    The not-deployed guard leaves a row due ON PURPOSE, so that a site which was
    charged and never went live is re-examined every tick instead of being billed
    for a 404. A site scheduled to close that never deployed hits both guards — and
    if the deploy check ran first it would be skipped forever: never charged, and
    never closed either, with the flag standing indefinitely."""
    ws = "ws-close-undeployed"
    await _fund(ws, 9000)
    doc = await _seed_site(
        workspace_id=ws,
        tier="staff",
        renewal_date=_yesterday(),
        deployed=False,
        cancels_at_period_end=True,
    )

    counts = await sweep_site_renewals()

    assert counts["closed"] == 1
    assert counts["not_live"] == 0
    assert (await Site.get(doc.id)).subscription_status == "none"
    assert await credits_service.balance(ws) == 9000
