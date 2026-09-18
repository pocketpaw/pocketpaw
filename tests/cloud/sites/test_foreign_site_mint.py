# tests/cloud/sites/test_foreign_site_mint.py — minting a Paw Bar concierge for a
# site PocketPaw does not host, as a PURCHASE rather than a bare credential row.
#
# WHY THIS EXISTS. ``mint_foreign_site`` wrote a Site and stopped there, and the
# concierge it existed to create never worked: an unstamped row resolves to the
# free floor, ``free`` does not sell the concierge, and the key resolver answers
# every visitor with a 403. So the assertions here are deliberately about the
# RESOLVER'S ANSWER and about the WALLET, not about the fields — a version that
# stamps ``plan_tier`` and forgets to activate passes any field-shaped test and
# still ships a concierge that refuses to talk.
#
# The four that are worth the most:
#
#   * ``test_a_minted_concierge_is_entitled_and_charged_once`` — the feature, end
#     to end: one debit, and ``concierge_entitled`` answers True.
#   * ``test_a_failed_charge_leaves_no_site`` — a paid tier with no money behind
#     it. It is the exact state removing the gateway set out to end, and a row
#     that survives a failed debit is indistinguishable from a real purchase.
#   * ``test_an_unverified_origin_refuses_before_anything_is_written`` — minting
#     on a domain the workspace cannot prove it controls. Asserted as no row AND
#     no debit, because "it raised" is also what a version that half-ran does.
#   * ``test_the_renewal_sweep_recharges_a_foreign_site_without_deploying`` — a
#     foreign site is undeployed forever, and the sweep's "never charge a site
#     that is not up" guard would otherwise hand it away free every month.
#
# tests/mutations/foreign_mint_purchase.json is the other half of this file: it
# deletes the status flip and the fail-closed rollback on purpose and expects
# these tests to notice.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden, InsufficientCredits, ValidationError
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.entitlements import service as entitlements_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

pytestmark = pytest.mark.anyio

_HOST = "brewco.example"
_OWNER = "user:maya"
_OWNED_POCKET = {"name": "Shop", "rippleSpec": {}}

# What a foreign concierge costs, read off the catalog rather than written as 19
# here: a test that hard-codes the price stops testing the purchase and starts
# testing the number, and re-pricing the ladder should not turn this file red.
_STAFF = site_plans.site_scoped_tier("staff")
assert _STAFF is not None
_PRICE_CREDITS = _STAFF.monthly_price_usd * 100


async def _verify_origin(workspace_id: str, host: str = _HOST) -> SiteOriginClaim:
    """Seed a VERIFIED ownership claim, the state ``verify_origin`` leaves behind.

    Written directly rather than driven through the probe: this suite is about
    what the mint does with a proof, and tests/ee/sites/test_domain_ownership.py
    already owns how a proof is obtained.
    """
    now = datetime.now(UTC)
    claim = SiteOriginClaim(
        workspace=workspace_id,
        host=host,
        token=f"pawverify-{uuid4().hex}",
        status="verified",
        issued_at=now,
        expires_at=now + timedelta(days=7),
        verified_at=now,
        method="well-known",
        issued_by=_OWNER,
    )
    await claim.insert()
    return claim


async def _fund(workspace_id: str, credits: int) -> None:
    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}",
    )


async def _mint(workspace_id: str, **overrides):
    """Call the mint with the pocket ownership check mocked to an owned pocket."""
    kwargs = dict(
        workspace_id=workspace_id,
        pocket_id="pk-1",
        owner=_OWNER,
        allowed_origins=[f"https://{_HOST}"],
        name="Brew Co Concierge",
    )
    kwargs.update(overrides)
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_OWNED_POCKET),
    ):
        return await sites_service.mint_foreign_site(**kwargs)


def _entitlements(site: Site):
    return entitlements_service.resolve_site_entitlements(
        site_id=str(site.id),
        workspace_id=site.workspace,
        plan_tier=site.plan_tier,
        subscription_status=site.subscription_status,
        concierge_enabled=site.concierge_enabled,
    )


# --------------------------------------------------------------------------- #
# 1. The feature — one charge, and a concierge that actually resolves
# --------------------------------------------------------------------------- #


async def test_a_minted_concierge_is_entitled_and_charged_once(mongo_db):  # noqa: ARG001
    """THE POINT OF THE SLICE. The row is not the deliverable; the entitlement is.

    So this asks the RESOLVER rather than reading the fields back: the mint's job
    is to leave a site whose ``concierge_entitled`` is True, and that is the AND of
    a rung that sells the concierge with an active subscription. Either half
    missing looks like a healthy row and serves a 403 to every visitor."""
    ws = "ws-mint-ok"
    await _fund(ws, 5000)
    await _verify_origin(ws)

    site = await _mint(ws)

    assert _entitlements(site).concierge_entitled is True, "the concierge must actually resolve"
    assert await credits_service.balance(ws) == 5000 - _PRICE_CREDITS, "charged exactly once"

    # And it survives the round trip, because the resolver reads the collection.
    stored = await Site.get(str(site.id))
    assert stored is not None
    assert _entitlements(stored).concierge_entitled is True
    assert stored.plan_tier == "staff"
    assert stored.subscription_status == "active"
    assert stored.period_paid_usd == _STAFF.monthly_price_usd
    assert stored.foreign_origin is True
    assert stored.script_name == "" and stored.deployed is False, "nothing was deployed"
    assert stored.allowed_origins == [_HOST], "the origin is normalized to a bare host"


async def test_the_charge_is_attributed_and_priced_from_the_catalog(mongo_db):  # noqa: ARG001
    """One ledger row, for the catalog's price, attributed to the buyer.

    A debit nobody can attribute is a support ticket nobody can answer, and a
    price written at the call site is a price that drifts from the storefront."""
    ws = "ws-mint-ledger"
    await _fund(ws, 5000)
    await _verify_origin(ws)

    with patch(
        "pocketpaw_ee.cloud.billing.service.charge_site_plan_credits",
        new=AsyncMock(return_value=0),
    ) as charge:
        site = await _mint(ws)

    charge.assert_awaited_once()
    call = charge.await_args.kwargs
    assert call["workspace_id"] == ws
    assert call["site_id"] == str(site.id)
    assert call["tier_key"] == "staff"
    assert call["amount_usd"] == _STAFF.monthly_price_usd
    assert call["member_id"] == _OWNER


# --------------------------------------------------------------------------- #
# 2. Fail closed — no row survives a charge that did not go through
# --------------------------------------------------------------------------- #


async def test_a_failed_charge_leaves_no_site(mongo_db):  # noqa: ARG001
    """A PAID TIER WITH NO MONEY BEHIND IT is the state this must never produce.

    An empty wallet raises 402 and the insert must be undone. A row left behind
    carries ``plan_tier="staff"`` and is byte-identical to one somebody paid for,
    so nothing downstream could ever tell them apart."""
    ws = "ws-mint-broke"
    await _verify_origin(ws)  # funded with nothing at all

    with pytest.raises(InsufficientCredits):
        await _mint(ws)

    assert await Site.find_one(Site.workspace == ws) is None, "no row survives a failed charge"
    assert await credits_service.balance(ws) == 0


async def test_a_charge_that_errors_for_any_other_reason_also_leaves_no_site(
    mongo_db,  # noqa: ARG001
):
    """The rollback is not keyed on ``InsufficientCredits``.

    A ledger outage leaves exactly the same unpaid row as an empty wallet, and a
    rollback that only handles the 402 would let the rarer failure ship the bug."""
    ws = "ws-mint-ledger-down"
    await _fund(ws, 5000)
    await _verify_origin(ws)

    boom = AsyncMock(side_effect=RuntimeError("ledger unavailable"))
    with patch("pocketpaw_ee.cloud.billing.service.charge_site_plan_credits", new=boom):
        with pytest.raises(RuntimeError):
            await _mint(ws)

    assert await Site.find_one(Site.workspace == ws) is None
    assert await credits_service.balance(ws) == 5000, "nothing was taken"


# --------------------------------------------------------------------------- #
# 3. The origin gate — refuse before anything is written or charged
# --------------------------------------------------------------------------- #


async def test_an_unverified_origin_refuses_before_anything_is_written(mongo_db):  # noqa: ARG001
    """Minting on a domain the workspace cannot prove it controls.

    Asserted as NO ROW and NO DEBIT rather than as "it raised": a version that
    inserted first and refused afterwards raises too, and leaves the embed
    credential for somebody else's domain sitting in the collection."""
    ws = "ws-mint-unverified"
    await _fund(ws, 5000)
    # Deliberately no claim seeded.

    with pytest.raises(Forbidden) as exc:
        await _mint(ws)

    assert exc.value.code == "sites.origin_unverified"
    assert await Site.find_one(Site.workspace == ws) is None
    assert await credits_service.balance(ws) == 5000


async def test_a_proof_from_another_workspace_does_not_count(mongo_db):  # noqa: ARG001
    """The replay the (workspace, host) lookup exists to stop, asked at the mint.

    A verified host is verified FOR A TENANT. If the gate read the claim by host
    alone, one customer proving their own domain would open it to everybody."""
    await _verify_origin("ws-the-real-owner")
    ws = "ws-the-impostor"
    await _fund(ws, 5000)

    with pytest.raises(Forbidden) as exc:
        await _mint(ws)

    assert exc.value.code == "sites.origin_unverified"
    assert await Site.find_one(Site.workspace == ws) is None


async def test_every_origin_must_be_proved_not_just_one(mongo_db):  # noqa: ARG001
    """ALL of them, not any of them.

    A single unproved entry on an otherwise legitimate allowlist is still a live
    concierge embedded on a domain that is not the buyer's, and an ``any`` here
    would be trivially satisfied by the buyer's own real domain."""
    ws = "ws-mint-mixed"
    await _fund(ws, 5000)
    await _verify_origin(ws)

    with pytest.raises(Forbidden) as exc:
        await _mint(ws, allowed_origins=[f"https://{_HOST}", "https://someone-else.example"])

    assert exc.value.code == "sites.origin_unverified"
    assert "someone-else.example" in exc.value.message
    assert await Site.find_one(Site.workspace == ws) is None


async def test_an_empty_origin_list_is_refused(mongo_db):  # noqa: ARG001
    """``origin_allowed`` fails closed on an empty list, so such a site could never
    serve its concierge anywhere — and would have been charged for the privilege."""
    ws = "ws-mint-no-origin"
    await _fund(ws, 5000)

    with pytest.raises(ValidationError) as exc:
        await _mint(ws, allowed_origins=["   "])

    assert exc.value.code == "sites.origin_required"
    assert await credits_service.balance(ws) == 5000


async def test_the_pocket_gate_still_runs_first(mongo_db):  # noqa: ARG001
    """A cross-tenant pocket is refused by the ownership check, ahead of the
    origin gate — the resolved CONCIERGE context would otherwise read the victim
    pocket's KB, which is the worse of the two leaks and should be named as such
    in the error the caller sees."""
    ws = "ws-mint-cross-tenant"
    await _fund(ws, 5000)
    await _verify_origin(ws)

    denied = AsyncMock(side_effect=Forbidden("pocket.access_denied", "no access"))
    with patch("pocketpaw_ee.cloud.pockets.service.get", new=denied):
        with pytest.raises(Forbidden) as exc:
            await sites_service.mint_foreign_site(
                workspace_id=ws,
                pocket_id="pk-victim",
                owner=_OWNER,
                allowed_origins=[f"https://{_HOST}"],
            )

    assert exc.value.code == "pocket.access_denied"
    assert await Site.find_one(Site.workspace == ws) is None
    assert await credits_service.balance(ws) == 5000


# --------------------------------------------------------------------------- #
# 4. The rail — credits, and no plan slot
# --------------------------------------------------------------------------- #


async def test_the_rail_is_credits_and_takes_no_plan_slot(mongo_db):  # noqa: ARG001
    """``billing_rail`` is set EXPLICITLY, and it is not the plan rail.

    Empty would leave it indistinguishable from the pre-cutover Dodo rows the
    add-on cart still invoices — a second charge for a month the wallet bought.
    ``plan`` would make the workspace's own slot arithmetic count a site nobody's
    subscription is carrying, so a customer would pay for this one and lose an
    included one."""
    from pocketpaw_ee.cloud.models.workspace import Workspace

    workspace = Workspace(name="Acme", slug=f"acme-{uuid4().hex}", owner=_OWNER, plan="go")
    await workspace.insert()
    ws = str(workspace.id)
    await _fund(ws, 5000)
    await _verify_origin(ws)

    site = await _mint(ws)

    assert site.billing_rail == "credits"
    assert site.renewal_date is not None, "the sweep has to be able to find it"
    used, allowance = await sites_service.plan_site_slots(ws)
    assert (used, allowance) == (0, 1), "a foreign site eats none of the plan's slots"


async def test_the_plan_reconciler_leaves_a_foreign_site_alone(mongo_db):  # noqa: ARG001
    """A downgrade releases PLAN-CARRIED sites to the free floor. A foreign site
    was bought with money, so releasing it would revoke a concierge the customer
    is paying for — silently, on somebody else's plan change."""
    from pocketpaw_ee.cloud.models.workspace import Workspace

    workspace = Workspace(name="Acme", slug=f"acme-{uuid4().hex}", owner=_OWNER, plan="free")
    await workspace.insert()
    ws = str(workspace.id)
    await _fund(ws, 5000)
    await _verify_origin(ws)

    site = await _mint(ws)
    # The free plan carries zero sites, so a reconcile here releases everything it
    # considers carried — which must be nothing.
    result = await sites_service.reconcile_plan_carried_sites(ws)

    assert result == {"carried": 0, "released": 0}
    stored = await Site.get(str(site.id))
    assert stored is not None
    assert stored.billing_rail == "credits"
    assert _entitlements(stored).concierge_entitled is True, "it keeps what it paid for"


# --------------------------------------------------------------------------- #
# 5. The renewal — charged monthly, and nothing is ever deployed
# --------------------------------------------------------------------------- #


async def test_the_renewal_sweep_recharges_a_foreign_site_without_deploying(
    mongo_db,  # noqa: ARG001
):
    """A foreign site is undeployed FOREVER, and the sweep refuses to charge an
    undeployed site — for good reason, since that shape normally means a deploy
    that failed. Without the exception this one is given away free every month.

    The second half of the assertion is the one that would not survive a careless
    fix: the sweep must keep being a debit and a date. A renewal that re-charged
    by way of a redeploy would fail on a site with no Worker to redeploy."""
    ws = "ws-foreign-renewal"
    await _fund(ws, 10_000)
    await _verify_origin(ws)
    site = await _mint(ws)

    # Make it due, the way a month passing would.
    site.renewal_date = datetime.now(UTC) - timedelta(days=1)
    await site.save()
    after_purchase = await credits_service.balance(ws)

    exploded: list[str] = []

    async def _must_not_deploy(**kwargs):
        exploded.append("deploy")
        raise AssertionError("the renewal sweep tried to deploy a foreign site")

    with patch.object(sites_service, "_deploy_site_doc", new=_must_not_deploy):
        counts = await sweep_site_renewals()

    assert counts["renewed"] == 1, "the month was charged"
    assert counts["not_live"] == 0, "a foreign site is not a broken deploy"
    assert exploded == []
    assert await credits_service.balance(ws) == after_purchase - _PRICE_CREDITS

    stored = await Site.get(str(site.id))
    assert stored is not None
    assert stored.renewal_date is not None
    stepped = stored.renewal_date
    stepped = stepped if stepped.tzinfo is not None else stepped.replace(tzinfo=UTC)
    assert stepped > datetime.now(UTC), "the next charge is a month out"
    assert stored.deployed is False and stored.script_name == ""
    assert _entitlements(stored).concierge_entitled is True


async def test_a_hosted_site_that_never_deployed_is_still_not_charged(mongo_db):  # noqa: ARG001
    """The exception is narrow, and this is the guard it must not widen.

    An ordinary site marked active that never deployed is an operator problem, not
    another month's debit. It reaches the sweep in exactly the shape a foreign site
    does apart from one flag, so if the skip were derived from ``deployed`` and
    ``script_name`` instead of the stamp, this row would start being billed."""
    ws = "ws-hosted-broken"
    await _fund(ws, 10_000)
    await Site(
        workspace=ws,
        pocket_id="pk-broken",
        owner=_OWNER,
        name="Never deployed",
        deployed=False,
        script_name="",
        url="",
        plan_tier="staff",
        subscription_status="active",
        billing_rail="credits",
        renewal_date=datetime.now(UTC) - timedelta(days=1),
        period_paid_usd=_STAFF.monthly_price_usd,
    ).insert()

    counts = await sweep_site_renewals()

    assert counts["not_live"] == 1 and counts["renewed"] == 0
    assert await credits_service.balance(ws) == 10_000, "nothing was charged"


# --------------------------------------------------------------------------- #
# 6. The shared activation helpers
# --------------------------------------------------------------------------- #


async def test_the_shared_renewal_stamp_steps_one_calendar_month(mongo_db):  # noqa: ARG001
    """``_stamp_next_renewal`` is the one renewal-date writer both paths use, so
    the interval is asserted once, here, on a date that tells the wrong answers
    apart. A month from 31 January is 28 February; 365 days is next year, 90 days
    is May and a flat 30 days is 2 March — every plausible substitution lands
    somewhere this assertion does not.

    The plan-rail branch is the other half: a site the workspace plan carries was
    never bought, and a date on it hands the sweep a row to debit.
    """
    at = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)

    bought = Site(workspace="ws-stamp", pocket_id="pk", owner=_OWNER, billing_rail="credits")
    sites_service._stamp_next_renewal(bought, at=at)
    assert bought.renewal_date == datetime(2026, 2, 28, 12, 0, tzinfo=UTC)

    carried = Site(workspace="ws-stamp", pocket_id="pk", owner=_OWNER, billing_rail="plan")
    carried.renewal_date = at
    sites_service._stamp_next_renewal(carried, at=at)
    assert carried.renewal_date is None, "a plan-carried site is invisible to the sweep"
