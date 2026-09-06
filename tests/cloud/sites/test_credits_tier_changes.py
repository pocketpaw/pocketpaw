# tests/cloud/sites/test_credits_tier_changes.py — what a CHANGE of tier costs,
# and what an empty wallet does at every point one can be empty.
#
# WHY THIS EXISTS. The credits rail shipped correct for the case it was written
# for — a first purchase — and wrong for every case after it. One line priced a
# tier change at the new tier's full month, which is three different overcharges
# wearing one coat:
#
#   * a DOWNGRADE took MORE money than the plan it was leaving ($19 staff down to
#     $7 site charged $7 on the spot, for less product);
#   * flipping between two tiers in an afternoon charged a month per flip, so a
#     customer could drain a funded wallet by changing their mind four times;
#   * an upgrade charged a second full month for a period already bought, then
#     reset ``renewal_date`` and handed back a free month, which hid it.
#
# The fix is one stored number — ``Site.period_paid_usd``, the most expensive tier
# already bought for the CURRENT period — and one subtraction against it. These
# tests are the arithmetic, because the arithmetic is the product: every case here
# is a real amount of a real customer's money.
#
# The three that are worth the most:
#
#   * ``test_a_downgrade_costs_nothing`` — paying more to get less is the one a
#     customer notices, screenshots and posts.
#   * ``test_flipping_between_tiers_cannot_drain_the_wallet`` — the unbounded one.
#     Every other bug here overcharges by a fixed amount; this one overcharges as
#     many times as the buyer clicks.
#   * ``test_a_legacy_rail_site_cannot_change_tier_here`` — the double BILL. A
#     site sold before the cutover is still invoiced by Dodo every month and
#     nothing left in the codebase can stop it, so charging the wallet as well
#     takes two payments for one site.
#
# Created 2026-09-05 (fix/sites-plan-credits): new test module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import ConflictError, InsufficientCredits
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

pytestmark = pytest.mark.anyio


class _Generator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


def _local_deploy(monkeypatch) -> list[str]:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: _Generator())
    from pocketpaw_ee.sites import local_server

    deploys: list[str] = []

    def _fake_deploy_local(site_id, project_dir, **kw):
        deploys.append(site_id)
        return f"http://local/{site_id}/"

    monkeypatch.setattr(local_server, "deploy_local", _fake_deploy_local)
    return deploys


# THE WORKSPACE PLAN MATTERS NOW, and this fixture pins it deliberately.
#
# Two facts collide (2026-09-06, feat/plan-included-sites): Paw Go / Pro / Pro Max
# CARRY sites — 1 / 3 / 10, at ``staff`` quality for no money — and the ``sites``
# feature itself starts at Go, so a ``free`` workspace cannot publish one at all.
# Between them the credit wallet is reached in exactly one situation: a workspace
# that has used up the sites its plan carries. So the workspace here is on ``go``
# with its ONE slot already filled. That is the only population this rail has
# left, and a fixture that skipped it would test a path no customer can reach.
async def _make_workspace() -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme",
        slug=f"acme-tiers-{datetime.now(UTC).timestamp()}",
        owner="u1",
        plan="go",
    )
    await ws.insert()
    await _fill_plan_slots(str(ws.id))
    return str(ws.id)


async def _fill_plan_slots(workspace_id: str, count: int = 1) -> None:
    """Occupy ``count`` of the workspace's plan-carried site slots.

    Decoy sites on their own pocket ids, so they never collide with the site under
    test. Stamped exactly as a carried site is — plan rail, staff tier, active, no
    renewal date — because ``plan_site_slots`` counts on ``billing_rail`` and a
    decoy with the wrong shape leaves the slot open and sends the test down the
    free rail while looking like it did the opposite."""
    from pocketpaw_ee.cloud.models.site import Site as _S

    for i in range(count):
        await _S(
            workspace=workspace_id,
            pocket_id=f"decoy-{workspace_id}-{i}",
            owner="u1",
            name=f"Decoy {i}",
            deployed=True,
            url="http://local/decoy/",
            plan_tier="staff",
            subscription_status="active",
            billing_rail="plan",
        ).insert()


async def _make_pocket(workspace_id: str) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name="My Landing",
        owner="u1",
        type="site",
        pattern="landing",
    )
    await doc.insert()
    return str(doc.id)


async def _fund(workspace_id: str, credits: int) -> None:
    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}",
    )


def _price(key: str) -> int:
    """The tier's monthly price in credits (1 credit == 1 cent)."""
    tier = site_plans.get_site_plan(key)
    assert tier is not None
    return tier.monthly_price_usd * 100


async def _publish(workspace_id: str, pocket_id: str, tier_key: str | None) -> Site:
    return await sites_service.publish_pocket(
        workspace_id=workspace_id,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key=tier_key,
        purchase_authorized=True,
        _bundle_reader=lambda d: b"bundle",
    )


async def _site(workspace_id: str) -> Site:
    docs = await Site.find(Site.workspace == workspace_id).to_list()
    # Never the slot-filling decoy: it is deployed and active, so a test asserting
    # a refused publish left nothing live would read it and pass.
    real = [d for d in docs if not str(d.pocket_id).startswith("decoy-")]
    assert len(real) == 1, f"expected one site under test, found {len(real)}"
    return real[0]


def _aware(value: datetime) -> datetime:
    """Re-attach UTC to a stamp read back from the database.

    BSON carries no timezone, so a datetime written as aware comes back naive and
    comparing it against ``datetime.now(UTC)`` raises instead of failing."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# What a tier change costs
# --------------------------------------------------------------------------- #


async def test_an_upgrade_charges_only_the_difference(mongo_db, monkeypatch):  # noqa: ARG001
    """$7 site to $19 staff costs $12, not $19.

    The period has already been paid for at the lower tier. Charging the new
    tier's full month bills that period twice over and is what this replaced."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "site")
    after_purchase = await credits_service.balance(ws)
    assert after_purchase == 9000 - _price("site")

    await _publish(ws, pocket_id, "staff")

    spent_on_upgrade = after_purchase - await credits_service.balance(ws)
    assert spent_on_upgrade == _price("staff") - _price("site")
    doc = await _site(ws)
    assert doc.plan_tier == "staff"
    # And the high-water mark moved, so the NEXT change prices against staff.
    assert doc.period_paid_usd == 19


async def test_a_downgrade_costs_nothing(mongo_db, monkeypatch):  # noqa: ARG001
    """Moving DOWN a tier must never take money.

    It charged the lower tier's full month — so a customer dropping from $19 to
    $7 to spend less was billed $7 immediately for the privilege, on top of the
    $19 they had already paid for the period they were still inside."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    after_purchase = await credits_service.balance(ws)

    await _publish(ws, pocket_id, "site")

    assert await credits_service.balance(ws) == after_purchase
    doc = await _site(ws)
    assert doc.plan_tier == "site"
    # The high-water mark does NOT fall. The customer paid for staff and owns it
    # until the period ends, which is what makes going back up free below.
    assert doc.period_paid_usd == 19


async def test_returning_to_a_tier_already_bought_this_period_is_free(mongo_db, monkeypatch):  # noqa: ARG001
    """Down to $7 and back up to $19 inside one period charges nothing the second
    time. The month of staff was bought the first time and has not expired."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    await _publish(ws, pocket_id, "site")
    after_downgrade = await credits_service.balance(ws)

    await _publish(ws, pocket_id, "staff")

    assert await credits_service.balance(ws) == after_downgrade
    assert (await _site(ws)).plan_tier == "staff"


async def test_flipping_between_tiers_cannot_drain_the_wallet(mongo_db, monkeypatch):  # noqa: ARG001
    """THE UNBOUNDED ONE. Every other overcharge here is a fixed amount; this one
    scales with how many times the buyer changes their mind.

    Four changes in one afternoon used to cost four months. The whole sequence
    must cost exactly one month of the most expensive tier it ever touched."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    for tier_key in ("site", "staff", "site", "staff", "site", "staff"):
        await _publish(ws, pocket_id, tier_key)

    spent = 9000 - await credits_service.balance(ws)
    assert spent == _price("staff"), (
        "six tier changes must cost one month of the dearest tier touched"
    )


async def test_an_upgrade_does_not_extend_the_paid_period(mongo_db, monkeypatch):  # noqa: ARG001
    """A tier change re-prices the period. It does not restart it.

    Resetting ``renewal_date`` to "a month from now" on every upgrade handed back
    a free month each time, which is also what disguised the double charge: the
    customer paid twice and got a later renewal, so the balance looked explicable."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "site")
    renewal_at_purchase = _aware((await _site(ws)).renewal_date)

    # Move the clock forward by moving the anchor back: the site is now 20 days
    # into its month, with 10 to run.
    doc = await _site(ws)
    doc.renewal_date = renewal_at_purchase - timedelta(days=20)
    await doc.save()
    due_before_upgrade = _aware((await _site(ws)).renewal_date)

    await _publish(ws, pocket_id, "staff")

    assert _aware((await _site(ws)).renewal_date) == due_before_upgrade


async def test_an_upgrade_on_a_short_wallet_changes_nothing(mongo_db, monkeypatch):  # noqa: ARG001
    """A refused upgrade must leave the site exactly as it was — still live, still
    on the tier it paid for. The charge runs BEFORE the stamp for this reason; the
    other order leaves a site claiming a tier no money was taken for."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    # Exactly enough for the cheap tier and not a credit more.
    await _fund(ws, _price("site"))
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "site")
    assert await credits_service.balance(ws) == 0

    with pytest.raises(InsufficientCredits):
        await _publish(ws, pocket_id, "staff")

    doc = await _site(ws)
    assert doc.plan_tier == "site", "a refused upgrade must not stamp the new tier"
    assert doc.subscription_status == "active"
    assert doc.deployed is True, "the site the customer paid for stays up"
    assert doc.period_paid_usd == 7
    assert await credits_service.balance(ws) == 0


async def test_exactly_enough_credits_buys_the_plan(mongo_db, monkeypatch):  # noqa: ARG001
    """The boundary. A wallet holding the price to the credit must succeed and
    land on zero — an off-by-one in the strict check would refuse a customer who
    topped up the exact amount the storefront quoted them."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, _price("staff"))
    pocket_id = await _make_pocket(ws)

    doc = await _publish(ws, pocket_id, "staff")

    assert await credits_service.balance(ws) == 0
    assert doc.subscription_status == "active"
    assert doc.deployed is True


# --------------------------------------------------------------------------- #
# Leaving a paid plan
# --------------------------------------------------------------------------- #


async def test_downgrading_to_free_schedules_the_close_for_the_period_end(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Cancelling does not take back the month the customer already bought.

    Two wrong answers bracket this one. Leaving the subscription "active" on a $0
    tier — what the code did before — describes a free site as a plan somebody is
    paying for, and the renewal sweep wakes on it every tick. Closing it on the
    spot, which was the first fix, forfeits the rest of a paid month: cancel on
    the 2nd and you lose 28 days AND pay a full month again to come back, because
    the high-water mark is gone.

    So the plan is SCHEDULED to close. Everything the customer bought stands —
    tier, active status, renewal date, paid mark — and the sweep closes it when
    the period runs out."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "site")
    renewal = (await _site(ws)).renewal_date
    await _publish(ws, pocket_id, "free")

    doc = await _site(ws)
    assert doc.plan_cancels_at_period_end is True
    # The paid tier is what carries the entitlements, so it has to survive the
    # cancellation too — a row flipped to "free" here loses the custom domain and
    # the badge removal mid-month while still holding an "active" subscription.
    assert doc.plan_tier == "site"
    assert doc.subscription_status == "active"
    assert doc.renewal_date == renewal
    assert doc.period_paid_usd == 7
    assert doc.deployed is True, "cancelling must not take the site down"


async def test_a_scheduled_close_costs_nothing_and_can_be_resumed(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Cancelling is free, and so is changing your mind inside the paid period.

    Resuming arrives as a SAME-TIER republish, because the plan selector still
    shows the tier the site holds — the one shape a tier change is written to
    ignore. Handled separately for that reason, and charged nothing: the month was
    bought once and the renewal that was about to close the plan now renews it."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    after_purchase = await credits_service.balance(ws)

    await _publish(ws, pocket_id, "free")
    assert (await _site(ws)).plan_cancels_at_period_end is True
    assert await credits_service.balance(ws) == after_purchase, "cancelling is free"

    await _publish(ws, pocket_id, "staff")

    doc = await _site(ws)
    assert doc.plan_cancels_at_period_end is False
    assert doc.plan_tier == "staff"
    assert doc.subscription_status == "active"
    assert doc.period_paid_usd == 19
    assert await credits_service.balance(ws) == after_purchase, "resuming is free too"


async def test_an_unauthorized_republish_does_not_resume_a_cancelled_plan(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Resuming re-arms a recurring charge, so it is an admin's decision.

    It must not be refused, though: a same-tier republish is the daily content
    edit of the person who builds the site, and erroring here would break it for
    every site with a cancellation pending. It publishes, and the close stays
    scheduled."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    await _publish(ws, pocket_id, "free")

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u2",
        pocket_id=pocket_id,
        site_plan_key="staff",
        _bundle_reader=lambda d: b"x",
    )

    assert doc.deployed is True, "a member's content edit still ships"
    assert (await _site(ws)).plan_cancels_at_period_end is True


async def test_an_upgrade_calls_off_a_scheduled_close(mongo_db, monkeypatch):  # noqa: ARG001
    """Paying to move up is a decision to keep paying. Left scheduled, the sweep
    would close the plan at the very renewal the admin just paid to change."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "site")
    await _publish(ws, pocket_id, "free")
    assert (await _site(ws)).plan_cancels_at_period_end is True

    await _publish(ws, pocket_id, "staff")

    doc = await _site(ws)
    assert doc.plan_cancels_at_period_end is False
    assert doc.plan_tier == "staff"


async def test_buying_again_after_going_free_pays_in_full(mongo_db, monkeypatch):  # noqa: ARG001
    """The other side of clearing the high-water mark. A site that left its plan
    and comes back is buying a NEW period, so it owes that month in full.

    Bought at the DEARER tier and re-bought at the cheaper one, because that is
    the direction the stale mark would have shown up in: a $19 mark carried past
    the cancellation makes the later $7 purchase compute a negative difference and
    charge nothing at all."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    # Cancel, then let the paid period actually run out — the close is scheduled,
    # so the mark is not cleared until the sweep reaches the renewal date. Buying
    # again BEFORE that is a different case entirely (it resumes, free), and it is
    # covered by ``test_a_scheduled_close_costs_nothing_and_can_be_resumed``.
    await _publish(ws, pocket_id, "free")
    doc = await _site(ws)
    doc.renewal_date = datetime.now(UTC) - timedelta(minutes=1)
    await doc.save()
    await sweep_site_renewals()
    after_free = await credits_service.balance(ws)
    assert (await _site(ws)).period_paid_usd == 0, "the closed period is not pre-paid"

    await _publish(ws, pocket_id, "site")

    assert after_free - await credits_service.balance(ws) == _price("site")
    doc = await _site(ws)
    assert doc.period_paid_usd == 7
    assert doc.subscription_status == "active"
    assert doc.renewal_date is not None


async def test_rebuying_the_same_tier_the_same_day_is_not_charged_twice(
    mongo_db, monkeypatch
):  # noqa: ARG001
    """WRITTEN DOWN BECAUSE IT LOOKS LIKE A LEAK AND IS NOT.

    Cancelling to the free floor and immediately re-buying the SAME tier on the
    SAME day takes no second payment: the debit's idempotency key is
    (site, tier, day), so the second purchase replays the first rather than
    charging it. That is the correct answer to the question actually being asked
    — the customer bought a month of staff this morning, nothing was refunded
    when they dropped to free, and they still hold a month of staff. Charging
    again would be selling the same month twice.

    What they do gain is the few hours between the two publishes, because the
    renewal date is re-anchored to the second one. It is bounded by a single day
    and cannot be repeated tomorrow, when the key's date rolls over and the
    purchase is charged in full.

    The same key is what stops a retried or double-submitted publish from taking
    two payments, which is the case it was written for."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    after_purchase = await credits_service.balance(ws)
    await _publish(ws, pocket_id, "free")
    await _publish(ws, pocket_id, "staff")

    assert await credits_service.balance(ws) == after_purchase
    doc = await _site(ws)
    assert doc.plan_tier == "staff"
    assert doc.subscription_status == "active"


async def test_a_republish_without_a_plan_key_never_downgrades(mongo_db, monkeypatch):  # noqa: ARG001
    """"Leave the plan alone" and "put me on the free floor" are different
    requests, and only one of them is a downgrade.

    An ordinary content republish omits ``site_plan_key``. Reading that as a
    request for the free tier silently strips every paid capability from a
    customer who is still being billed, and nothing restores it."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    after_purchase = await credits_service.balance(ws)

    await _publish(ws, pocket_id, None)

    doc = await _site(ws)
    assert doc.plan_tier == "staff"
    assert doc.subscription_status == "active"
    assert doc.renewal_date is not None
    assert await credits_service.balance(ws) == after_purchase
    # AND NOT SCHEDULED TO END, which is where this misreading hides now that a
    # cancellation waits for the period to run out. The four assertions above are
    # every field an immediate close would have touched, so they all pass while a
    # content edit quietly books the customer's plan to end at its next renewal —
    # nothing visible until the month is up. Asserted separately for that reason.
    assert doc.plan_cancels_at_period_end is False


async def test_an_org_flat_is_not_read_as_a_downgrade(mongo_db, monkeypatch):  # noqa: ARG001
    """``studio`` is a workspace-wide flat, not a per-site tier, so it is not a
    legal ``site_plan_key``. It has a PRICE though, and it resolves in the
    catalog — so a downgrade check written against the price map rather than the
    per-site one would treat it as a request for the free floor and close a
    paying customer's subscription."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")

    await _publish(ws, pocket_id, "studio")

    doc = await _site(ws)
    assert doc.plan_tier == "staff", "an org key must not be stamped on one site"
    assert doc.subscription_status == "active"
    assert doc.renewal_date is not None


# --------------------------------------------------------------------------- #
# Sites left on the pre-cutover Dodo rails
# --------------------------------------------------------------------------- #


async def _seed_legacy(workspace_id: str, pocket_id: str, *, status: str) -> Site:
    """A site as the Dodo add-on rail left it: paid for, but not by the wallet.

    ``billing_rail`` is "" rather than "addon" on purpose — that is what every row
    written before the field looks like, and it is the population that actually
    exists in production."""
    doc = Site(
        id=sites_service._live_object_id(workspace_id, pocket_id),
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner="u1",
        name="My Landing",
        script_name="s",
        deployed=status == "active",
        signed_key="k",
        url="http://local/s/",
        plan_tier="site",
        subscription_status=status,
        billing_rail="",
    )
    await doc.insert()
    return doc


async def test_a_legacy_rail_site_cannot_change_tier_here(mongo_db, monkeypatch):  # noqa: ARG001
    """THE DOUBLE BILL. Dodo is still invoicing this site every month and nothing
    left in the codebase can adjust or cancel that line — ``sync_site_addons``
    went with the rest of the gateway. Charging the wallet as well takes two
    payments for one site, so the tier change is refused and routed to support."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)
    await _seed_legacy(ws, pocket_id, status="active")

    with pytest.raises(ConflictError) as exc:
        await _publish(ws, pocket_id, "staff")

    assert exc.value.code == "sites.legacy_billing_rail"
    assert await credits_service.balance(ws) == 9000, "nothing may be debited"
    assert (await _site(ws)).plan_tier == "site"


async def test_a_legacy_rail_site_still_republishes_free(mongo_db, monkeypatch):  # noqa: ARG001
    """The refusal is narrow on purpose. Editing content on the plan you already
    have is what the affected customers do every day, and it must keep working
    and stay free — on the legacy rail the gateway is already billing for it."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)
    await _seed_legacy(ws, pocket_id, status="active")

    await _publish(ws, pocket_id, "site")

    assert await credits_service.balance(ws) == 9000
    doc = await _site(ws)
    assert doc.plan_tier == "site"
    assert doc.subscription_status == "active"


async def test_a_stuck_pending_legacy_site_publishes_on_credits(mongo_db, monkeypatch):  # noqa: ARG001
    """THE ROW THE WHOLE FIX IS FOR. A site that picked a paid plan under the old
    rails was left PENDING, undeployed, waiting on a payment that could not be
    made. It is not ``already_paying``, so the legacy refusal must not catch it —
    it takes the wallet rail like any new purchase and comes out on the credits
    rail, live."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)
    await _seed_legacy(ws, pocket_id, status="pending")

    await _publish(ws, pocket_id, "site")

    doc = await _site(ws)
    assert doc.subscription_status == "active"
    assert doc.deployed is True
    assert doc.billing_rail == "credits"
    assert doc.period_paid_usd == 7
    assert await credits_service.balance(ws) == 9000 - _price("site")


# --------------------------------------------------------------------------- #
# The charge landed and the deploy did not
# --------------------------------------------------------------------------- #


async def test_a_failed_deploy_is_recovered_by_republishing_for_free(
    mongo_db, monkeypatch
):  # noqa: ARG001
    """CHARGED, AND NOTHING WENT LIVE. What happens next has to be free.

    ``activate_site`` marks the subscription active before running the deploy, so
    a deploy that raises leaves the customer with a debited wallet and a site that
    is not up. The gateway webhook that used to retry the deploy is gone, so the
    only route back is the buyer republishing — and that route has to redeploy
    without taking a second month, because the month is already theirs.

    It does, and not by accident: the republish reads as ``already_paying`` at the
    same tier, so no charge branch is entered at all."""
    deploys = _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    from pocketpaw_ee.sites import local_server

    real_deploy = local_server.deploy_local
    fail_next = {"on": True}

    def _flaky_deploy(site_id, project_dir, **kw):
        if fail_next["on"]:
            fail_next["on"] = False
            raise RuntimeError("cloudflare said no")
        return real_deploy(site_id, project_dir, **kw)

    monkeypatch.setattr(local_server, "deploy_local", _flaky_deploy)

    with pytest.raises(RuntimeError):
        await _publish(ws, pocket_id, "site")

    charged_once = 9000 - await credits_service.balance(ws)
    assert charged_once == _price("site"), "the wallet was debited before the deploy"
    stuck = await _site(ws)
    assert stuck.deployed is False

    # The buyer republishes. Same tier, same period — nothing more to pay.
    await _publish(ws, pocket_id, "site")

    assert 9000 - await credits_service.balance(ws) == _price("site"), (
        "recovering from our failed deploy must not cost a second month"
    )
    recovered = await _site(ws)
    assert recovered.deployed is True
    assert recovered.subscription_status == "active"
    assert deploys, "the recovery republish actually deployed"


async def test_cancelling_a_site_with_no_renewal_date_closes_it_immediately(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """There has to be a period to honour before the close can wait for one.

    An active paid site with no ``renewal_date`` is not a shape the publish path
    writes — it is a legacy or half-healed row — and scheduling one would be a
    close nothing ever performs: the renewal sweep selects on that date, so a row
    without one is never examined again. The site would sit on a paid tier the
    customer cancelled, holding capabilities forever, with the flag standing.

    So a missing period end means there is nothing to honour, and it closes now."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    doc = await _site(ws)
    doc.renewal_date = None
    await doc.save()

    await _publish(ws, pocket_id, "free")

    fresh = await _site(ws)
    assert fresh.plan_cancels_at_period_end is False, "nothing to wait for"
    assert fresh.plan_tier == "free"
    assert fresh.subscription_status == "none"
    assert fresh.period_paid_usd == 0
