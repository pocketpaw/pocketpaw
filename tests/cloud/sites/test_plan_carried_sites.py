# tests/cloud/sites/test_plan_carried_sites.py — the WORKSPACE PLAN as a site
# rail: Paw Go carries 1 site, Pro 3, Pro Max 10, at ``staff`` quality and for no
# money.
#
# WHY THIS EXISTS. Until 2026-09-06 a paid site had exactly one price: the credit
# wallet debited $7 or $19 a month for it. Bundling sites into the workspace ladder
# adds a second answer to "what does this site cost", and the two answers are told
# apart by ONE field — ``Site.billing_rail``. Every way of getting that field wrong
# is a money bug in one direction or the other:
#
#   * a carried site stamped ``credits`` gets a ``renewal_date``, and the renewal
#     sweep bills a customer monthly for a site their subscription already covers;
#   * a bought site stamped ``plan`` never renews, and the customer keeps paid
#     capabilities they stopped paying for;
#   * a carried site that keeps ``period_paid_usd`` can be walked down a rung and
#     off the plan holding a month of credit for money nobody spent;
#   * and a workspace that DOWNGRADES keeps every slot it ever took, because the
#     allowance is checked when a site is published and never again.
#
# The three that are worth the most:
#
#   * ``test_a_carried_site_is_invisible_to_the_renewal_sweep`` — the monthly
#     double charge. It is the one a customer notices on a statement.
#   * ``test_a_downgrade_releases_the_sites_the_new_plan_cannot_carry`` — the leak.
#     Free hosting forever, and nothing in the product ever mentions it again.
#   * ``test_a_carried_site_is_never_charged_to_change_tier`` — the charge for a
#     DOWNGRADE on a site nobody is paying for, which is the shape the credits
#     arithmetic produces if it is allowed anywhere near this rail.
#
# Created 2026-09-06 (feat/plan-included-sites): new test module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

pytestmark = pytest.mark.anyio


class _Generator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


def _local_deploy(monkeypatch) -> None:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: _Generator())
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(local_server, "deploy_local", lambda sid, d, **kw: f"http://local/{sid}/")


async def _make_workspace(plan: str) -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    # ``uuid4`` and not a timestamp: several tests here mint two workspaces in a
    # row, and a slug from ``datetime.now()`` collides inside one clock tick (~15.6
    # ms on Windows). The second insert then either fails or resolves to the first.
    ws = Workspace(
        name="Acme",
        slug=f"acme-carry-{plan}-{uuid4().hex}",
        owner="u1",
        plan=plan,
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(workspace_id: str, name: str = "Landing") -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id, name=name, owner="u1", type="site", pattern="landing"
    )
    await doc.insert()
    return str(doc.id)


async def _publish(ws: str, pocket_id: str, tier_key: str | None, *, authorized: bool = True):
    return await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key=tier_key,
        purchase_authorized=authorized,
        _bundle_reader=lambda d: b"bundle",
    )


async def _fund(ws: str, credits: int = 9000) -> None:
    await credits_service.grant(
        workspace=ws, amount=credits, cause="top_up", idempotency_key=f"seed-{ws}-{credits}"
    )


async def _site_for(pocket_id: str) -> Site:
    doc = await Site.find_one(Site.pocket_id == pocket_id)
    assert doc is not None
    return doc


# --------------------------------------------------------------------------- #
# Taking a slot
# --------------------------------------------------------------------------- #


async def test_the_first_site_on_go_is_carried_not_bought(mongo_db, monkeypatch):  # noqa: ARG001
    """The feature. Paw Go includes a site, so publishing one costs nothing."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    doc = await _publish(ws, pocket_id, "staff")

    assert doc.billing_rail == "plan"
    assert doc.plan_tier == "staff"
    assert doc.subscription_status == "active"
    assert doc.deployed is True
    assert await credits_service.balance(ws) == 9000, "an included site debits nothing"
    # NOTHING IS PRE-PAID, and the 0 is load-bearing rather than tidy. It is the
    # number a later tier change subtracts from: leaving the tier's price here
    # would let someone walk a carried site down a rung, drop off the plan, and
    # come back holding a month of credit for money nobody spent.
    assert doc.period_paid_usd == 0
    assert doc.renewal_date is None


async def test_the_plan_grants_staff_even_when_a_cheaper_rung_is_asked_for(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Both rungs cost the buyer nothing here, so giving them the cheaper one only
    withholds the concierge their subscription already includes."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    pocket_id = await _make_pocket(ws)

    doc = await _publish(ws, pocket_id, "site")

    assert doc.plan_tier == "staff"
    assert doc.billing_rail == "plan"


async def test_the_site_past_the_allowance_falls_through_to_the_wallet(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """THE OVERFLOW. Go carries one site; the second is a purchase like any other,
    which is the only role the per-site ladder has left."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)
    first = await _make_pocket(ws, "First")
    second = await _make_pocket(ws, "Second")

    await _publish(ws, first, "staff")
    after_first = await credits_service.balance(ws)
    assert after_first == 9000, "the first is carried"

    await _publish(ws, second, "site")

    doc = await _site_for(second)
    assert doc.billing_rail == "credits"
    assert doc.plan_tier == "site", "the wallet buys the rung that was asked for"
    assert doc.renewal_date is not None
    assert await credits_service.balance(ws) == after_first - 700


async def test_a_wallet_bought_site_does_not_eat_a_plan_slot(mongo_db, monkeypatch):  # noqa: ARG001
    """The slot count reads the RAIL, not the tier, and the difference is a slot.

    A site the wallet bought also holds ``staff`` — that is what $19 buys. Counting
    slots by tier would let a workspace's purchased sites consume the ones its
    subscription grants ON TOP of them: pay for a site, lose an included one."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)

    # A site the WALLET bought, seeded directly so it exists before any slot is
    # taken — which is the ordering the publish path can never produce on its own.
    await Site(
        workspace=ws,
        pocket_id="bought-elsewhere",
        owner="u1",
        name="Bought",
        deployed=True,
        url="http://local/bought/",
        plan_tier="staff",
        subscription_status="active",
        billing_rail="credits",
        renewal_date=datetime.now(UTC) + timedelta(days=20),
        period_paid_usd=19,
    ).insert()

    used, allowance = await sites_service.plan_site_slots(ws)
    assert (used, allowance) == (0, 1), "a purchased site takes none of the plan's slots"

    pocket_id = await _make_pocket(ws)
    doc = await _publish(ws, pocket_id, "staff")

    assert doc.billing_rail == "plan"
    assert await credits_service.balance(ws) == 9000, "the included site is still free"


async def test_recovering_a_carried_site_that_never_deployed_is_still_free(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """A carried site whose deploy failed is republished to recover it — and the
    slot it is already holding must not count against it.

    Its subscription reads ``pending``, so it is not "already paying" and the
    publish takes the purchase branch again. If the slot count included the site
    itself, a one-slot workspace would read itself as full, fall through to the
    wallet, and CHARGE the customer to recover a site their plan carries."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    # The shape ``activate_site`` leaves behind when a deploy raises: on the rail,
    # holding the slot, but pending and not live.
    await _publish(ws, pocket_id, "staff")
    stranded = await _site_for(pocket_id)
    stranded.subscription_status = "pending"
    stranded.deployed = False
    await stranded.save()

    doc = await _publish(ws, pocket_id, "staff")

    assert doc.billing_rail == "plan"
    assert doc.deployed is True
    assert await credits_service.balance(ws) == 9000, "recovery is not a purchase"


async def test_pro_carries_three_and_charges_for_the_fourth(mongo_db, monkeypatch):  # noqa: ARG001
    """The count is the plan's, not a constant. Three carried, then the wallet."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("pro")
    await _fund(ws)
    pockets = [await _make_pocket(ws, f"Site {i}") for i in range(4)]

    for pid in pockets[:3]:
        await _publish(ws, pid, "staff")
    assert await credits_service.balance(ws) == 9000

    await _publish(ws, pockets[3], "staff")

    assert (await _site_for(pockets[3])).billing_rail == "credits"
    assert await credits_service.balance(ws) == 9000 - 1900


async def test_a_republish_does_not_consume_a_second_slot(mongo_db, monkeypatch):  # noqa: ARG001
    """A site holds the slot it is asking about.

    Counting itself would push a one-slot workspace over on its own second
    publish, drop the site onto the wallet, and charge for a content edit."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    await _publish(ws, pocket_id, "staff")

    doc = await _site_for(pocket_id)
    assert doc.billing_rail == "plan"
    assert await credits_service.balance(ws) == 9000
    used, allowance = await sites_service.plan_site_slots(ws)
    assert (used, allowance) == (1, 1)


# --------------------------------------------------------------------------- #
# What a carried site costs later — which is nothing, in every direction
# --------------------------------------------------------------------------- #


async def test_a_carried_site_is_invisible_to_the_renewal_sweep(mongo_db, monkeypatch):  # noqa: ARG001
    """THE MONTHLY DOUBLE CHARGE. The plan renews; the site does not.

    A ``renewal_date`` on this rail is all it takes: the sweep selects on
    ``renewal_date <= now`` and would bill the wallet every month for a site the
    subscription already covers. Forced due here, so a rail check that had quietly
    stopped working could not hide behind a date a month away."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    doc = await _publish(ws, pocket_id, "staff")
    assert doc.renewal_date is None, "a carried site is not on a charge cycle"

    fresh = await _site_for(pocket_id)
    fresh.renewal_date = datetime.now(UTC) - timedelta(days=1)
    await fresh.save()
    counts = await sweep_site_renewals()

    assert counts["renewed"] == 0
    assert await credits_service.balance(ws) == 9000


async def test_a_carried_site_is_never_charged_to_change_tier(mongo_db, monkeypatch):  # noqa: ARG001
    """A CHARGE FOR A DOWNGRADE, which is what the credits arithmetic produces if
    it is let anywhere near this rail.

    ``period_paid_usd`` is 0 on a carried site — nothing was paid — so moving to
    the $7 rung computes a $7 difference and debits the wallet to make a free site
    cheaper. The rail takes its own branch for exactly this reason."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    await _fund(ws)
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    await _publish(ws, pocket_id, "site")

    doc = await _site_for(pocket_id)
    assert await credits_service.balance(ws) == 9000
    assert doc.billing_rail == "plan"
    assert doc.plan_tier == "staff", "it keeps what the plan carries"


async def test_dropping_a_carried_site_to_free_releases_the_slot_at_once(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Unlike a credits cancellation, this one is immediate — and it must be.

    A credits plan waits for the end of the month it was paid for, because that
    month was bought. Nothing was bought here, so there is nothing to honour and
    nothing to forfeit; holding the slot open would only stop the workspace using
    it somewhere else."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    pocket_id = await _make_pocket(ws)

    await _publish(ws, pocket_id, "staff")
    await _publish(ws, pocket_id, "free")

    doc = await _site_for(pocket_id)
    assert doc.billing_rail == ""
    assert doc.subscription_status == "none"
    assert doc.plan_cancels_at_period_end is False
    assert doc.deployed is True, "releasing a slot must not take the site down"
    used, _ = await sites_service.plan_site_slots(ws)
    assert used == 0, "the slot is back"


async def test_taking_a_slot_needs_no_admin(mongo_db, monkeypatch):  # noqa: ARG001
    """The gate draws its line at MONEY, and a carried site spends none.

    ``sites.buy_plan`` exists because a member could commit the company to a
    recurring charge. Filling a slot the subscription already paid for commits it
    to nothing, and refusing would send someone to find an admin to approve
    spending zero."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    pocket_id = await _make_pocket(ws)

    doc = await _publish(ws, pocket_id, "staff", authorized=False)

    assert doc.billing_rail == "plan"
    assert doc.plan_tier == "staff"


# --------------------------------------------------------------------------- #
# Reconciling a plan that moved
# --------------------------------------------------------------------------- #


async def test_a_downgrade_releases_the_sites_the_new_plan_cannot_carry(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """THE LEAK. The allowance is checked when a site is published and never again.

    Three sites on Pro, then the subscription cancels to Go. Without a reconcile
    all three keep riding a plan that carries one — free hosting, permanently, and
    nothing in the product ever mentions it again."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("pro")
    pockets = [await _make_pocket(ws, f"Site {i}") for i in range(3)]
    for pid in pockets:
        await _publish(ws, pid, "staff")
    assert (await sites_service.plan_site_slots(ws))[0] == 3

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    assert await workspace_service.set_workspace_plan(ws, "go") is True

    used, allowance = await sites_service.plan_site_slots(ws)
    assert (used, allowance) == (1, 1)
    # OLDEST FIRST KEEPS ITS SLOT — the site most likely to be linked and indexed.
    assert (await _site_for(pockets[0])).billing_rail == "plan"
    for pid in pockets[1:]:
        doc = await _site_for(pid)
        assert doc.billing_rail == ""
        assert doc.subscription_status == "none"
        assert doc.deployed is True, "a released site stays live on the free floor"


async def test_reconciling_a_workspace_inside_its_allowance_changes_nothing(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Idempotent, and it has to be: it runs on every plan write, including the
    upgrades and the no-op re-writes of a plan the workspace already holds."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("pro")
    pocket_id = await _make_pocket(ws)
    await _publish(ws, pocket_id, "staff")

    first = await sites_service.reconcile_plan_carried_sites(ws)
    second = await sites_service.reconcile_plan_carried_sites(ws)

    assert first == {"carried": 1, "released": 0}
    assert second == {"carried": 1, "released": 0}
    assert (await _site_for(pocket_id)).billing_rail == "plan"


async def test_an_upgrade_reconciles_to_a_no_op_and_leaves_room(
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Moving UP must not release anything, and must open the new slots."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace("go")
    pocket_id = await _make_pocket(ws)
    await _publish(ws, pocket_id, "staff")

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    await workspace_service.set_workspace_plan(ws, "pro")

    used, allowance = await sites_service.plan_site_slots(ws)
    assert (used, allowance) == (1, 3)
    assert (await _site_for(pocket_id)).billing_rail == "plan"


async def test_a_workspace_on_free_carries_nothing(mongo_db):  # noqa: ARG001
    """Free carries no sites, and an unknown plan key must read the same way.

    Every other ceiling in the plan catalog fails closed to Free because an
    over-generous default is an overspend somebody can see. This one decides
    whether a site is billed AT ALL, so the generous default would be hosting
    nothing ever reclaims."""
    ws = await _make_workspace("free")
    assert (await sites_service.plan_site_slots(ws)) == (0, 0)

    bogus = await _make_workspace("platinum-unlimited")
    assert (await sites_service.plan_site_slots(bogus)) == (0, 0)


def test_the_catalog_fails_closed_on_a_plan_it_does_not_know():
    """Asserted on the CATALOG, because the resolver cannot reach this branch.

    ``resolve_entitlements`` maps an unrecognised plan string to the base tier
    before the catalog ever sees it, so a workspace on a typo'd plan is answered
    by Free's row and the ``_build`` default never runs. That default is still the
    thing to get right — anything that builds a tier by key hits it — and testing
    it through the resolver only proves the resolver's fallback, which is a
    different guard. Every other ceiling here fails closed because a generous
    default is an overspend somebody sees; this one would be free hosting nothing
    ever reclaims."""
    from pocketpaw_ee.cloud.billing import plans as plan_catalog

    tier = plan_catalog._build("platinum-unlimited")
    assert tier.included_sites == 0
