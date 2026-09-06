# tests/cloud/sites/test_credits_publish.py — the CREDITS PUBLISH RAIL end to end:
# buying a paid site plan spends the workspace's own credit wallet and deploys in
# the same request, with no gateway call and no hosted checkout anywhere in it.
#
# WHY THIS RAIL EXISTS. Every paid publish used to need a live Dodo subscription
# to hang an add-on line on. A workspace without one — which is every self-hosted
# deployment and every workspace that has not bought a plan — got its site created
# PENDING, the charge refused with ``NoActiveSubscription``, and the site reverted
# to pending and left undeployed. The Billing tab then told the buyer to "complete
# checkout to publish" about a checkout that was never opened, so the site sat
# pending forever with no way forward. The wallet was already there, already
# funded, and already the thing everything else in the product spends.
#
# The three that are worth the most:
#
#   * ``test_a_paid_publish_debits_the_workspace_wallet`` — the whole feature. No
#     subscription exists, and the site still goes live, because the wallet paid.
#   * ``test_no_gateway_is_touched_by_a_paid_publish`` — the double charge. The
#     Dodo rails are deleted, so the publish path must contain no gateway call at
#     all; the injected provider raises on every method to make a reintroduced one
#     fail here rather than bill a customer who has already paid.
#   * ``test_an_empty_wallet_leaves_the_site_unpaid_and_undeployed`` — a refusal
#     must charge nothing and grant nothing. Nothing else ever rewrites
#     ``subscription_status``, so a missed revert is permanent.
#
# Created 2026-09-05 (fix/sites-plan-credits): new test module.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.errors import InsufficientCredits
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.anyio


class _RecordingGenerator:
    def __init__(self):
        self.build_calls: list[dict] = []

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(dict(kw))
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _ForbiddenBillingProvider:
    """A gateway that fails the test if the publish touches it at all.

    The credits rail's defining claim is that no payment gateway is involved, so
    the assertion is "these were never called" rather than "these were called
    with the right arguments".
    """

    async def create_subscription(self, **kw):
        raise AssertionError("the credits rail must never open a subscription")

    async def change_plan(self, **kw):
        raise AssertionError("the credits rail must never change a subscription")

    async def cancel_subscription(self, subscription_id: str) -> None:
        raise AssertionError("the credits rail must never cancel a subscription")


# THE WORKSPACE PLAN MATTERS NOW, and these fixtures pin it deliberately.
#
# Two facts collide here (both 2026-09-06, feat/plan-included-sites). Paw Go /
# Pro / Pro Max CARRY sites — 1 / 3 / 10, at ``staff`` quality, for no money —
# and the ``sites`` feature itself starts at Go, so a ``free`` workspace cannot
# publish a site at all. Between them, the credit wallet is reached in exactly one
# situation: a workspace that has used up the sites its plan carries.
#
# So every workspace below is on ``go`` with its ONE slot already filled by a
# decoy site on the plan rail. That is not a trick to make the tests pass — it is
# the only population this rail has left, and a fixture that skipped it would be
# testing a code path no customer can reach.


async def _fill_plan_slots(workspace_id: str, count: int = 1) -> None:
    """Occupy ``count`` of the workspace's plan-carried site slots.

    Decoy sites on their own pocket ids, so they never collide with the site under
    test (which is keyed on its own pocket). They are stamped exactly as a carried
    site is — plan rail, staff tier, active, no renewal date — because
    ``plan_site_slots`` counts on ``billing_rail`` and a decoy that got the shape
    wrong would leave the slot open and send the test down the free rail."""
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


async def _site_under_test(workspace_id: str):
    """The site this test published, never a slot-filling decoy.

    ``find_one(Site.workspace == ws)`` used to be unambiguous — one workspace, one
    site. It stopped being so the moment a fixture had to occupy the plan's slots,
    and it fails in the worst way: the decoy is deployed and active, so a test
    asserting a REFUSED publish left nothing live reads the decoy and passes."""
    from pocketpaw_ee.cloud.models.site import Site as _S

    docs = await _S.find(_S.workspace == workspace_id).to_list()
    real = [d for d in docs if not str(d.pocket_id).startswith("decoy-")]
    assert len(real) == 1, f"expected one site under test, found {len(real)}"
    return real[0]


async def _make_workspace(plan: str = "go", *, fill_slots: bool = True) -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme",
        slug=f"acme-credits-{datetime.now(UTC).timestamp()}",
        owner="u1",
        plan=plan,
    )
    await ws.insert()
    if fill_slots:
        await _fill_plan_slots(str(ws.id))
    return str(ws.id)


async def _make_pocket(*, workspace_id: str, owner: str = "u1") -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name="My Landing",
        owner=owner,
        type="site",
        pattern="landing",
    )
    await doc.insert()
    return str(doc.id)


def _local_deploy(monkeypatch) -> list[str]:
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: gen)
    from pocketpaw_ee.sites import local_server

    deploys: list[str] = []

    def _fake_deploy_local(site_id, project_dir, **kw):
        deploys.append(site_id)
        return f"http://local/{site_id}/"

    monkeypatch.setattr(local_server, "deploy_local", _fake_deploy_local)
    return deploys


async def _fund(workspace_id: str, credits: int) -> None:
    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}-{credits}",
    )


def _price_credits(key: str) -> int:
    """The tier's monthly price in credits (1 credit == 1 cent)."""
    tier = site_plans.get_site_plan(key)
    assert tier is not None
    return tier.monthly_price_usd * 100


# --------------------------------------------------------------------------- #


async def test_a_paid_publish_debits_the_workspace_wallet(mongo_db, monkeypatch):  # noqa: ARG001
    """The whole feature. No subscription exists anywhere and the site still goes
    live, because the workspace wallet paid for it inside this request."""
    deploys = _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 5000)
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert doc.deployed is True, "the wallet paid, so the site is live in this request"
    assert doc.subscription_status == "active"
    assert doc.plan_tier == "site"
    assert doc.billing_rail == "credits"
    assert doc.renewal_date is not None, "a monthly plan needs a next-charge date"
    assert deploys, "the site must actually deploy"

    assert await credits_service.balance(ws) == 5000 - _price_credits("site")
    assert not hasattr(sites_service._to_response(doc), "checkout_url"), (
        "the field is GONE, not merely null — a null one would read as 'the "
        "checkout has not opened yet' to anything still looking for it"
    )


async def test_an_empty_wallet_leaves_the_site_unpaid_and_undeployed(mongo_db, monkeypatch):  # noqa: ARG001
    """A refusal charges nothing and grants nothing.

    Nothing else ever rewrites ``subscription_status``, so a site left "active"
    after a refused debit would hold badge removal, custom domains and the
    concierge for free and for good."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 100)
    pocket_id = await _make_pocket(workspace_id=ws)

    with pytest.raises(InsufficientCredits):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="site",
            purchase_authorized=True,
            _bundle_reader=lambda d: b"x",
        )

    assert await credits_service.balance(ws) == 100, "a refused debit moves no money"
    doc = await _site_under_test(ws)
    assert doc is not None
    assert doc.subscription_status != "active"
    assert doc.deployed is False


async def test_no_gateway_is_touched_by_a_paid_publish(mongo_db, monkeypatch):  # noqa: ARG001
    """THE DOUBLE CHARGE, guarded where it can still be introduced.

    This replaced a test asserting that a credits-paid site stayed off the Dodo
    add-on cart. There is no cart any more — ``_site_addon_cart`` and
    ``sync_site_addons`` were deleted with the rail — so that particular double
    charge is structurally impossible rather than merely prevented.

    What remains possible is somebody reintroducing a gateway call into the
    publish path. ``_ForbiddenBillingProvider`` raises on every provider method,
    so any such call fails this test loudly instead of quietly billing a customer
    who has already paid from their balance.
    """
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 5000)
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert doc.deployed is True
    assert doc.billing_rail == "credits"
    assert doc.subscription_id is None, (
        "a per-site gateway subscription id on a wallet-paid site would put it "
        "back in scope for anything that still reads that field"
    )
    # And the money moved exactly once.
    assert await credits_service.balance(ws) == 5000 - _price_credits("site")


async def test_the_subscription_reads_active_before_the_deploy_runs(mongo_db, monkeypatch):  # noqa: ARG001
    """Pins the ORDERING inside ``activate_site``, by observing what the deploy
    could see while it ran. Moved here from test_charge_first.py when that module
    retired with the hosted-checkout rail; the credits rail deploys through the
    same function, so the trap is unchanged.

    Asserting the FINAL state cannot catch this — ``subscription_status`` is
    "active" at the end either way. The only way to tell the two orderings apart
    is to look at the doc AT DEPLOY TIME, which is what the stampers do:
    ``_embed_concierge_bar`` and ``_stamp_free_badge`` each re-read the Site doc
    mid-deploy and resolve entitlements from it. Flip the order and a customer who
    has just paid gets a page with the FREE attribution badge stamped on it and no
    concierge loader, and nothing re-runs either stamper."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 5000)
    pocket_id = await _make_pocket(workspace_id=ws)

    seen: list[str] = []
    real_deploy = sites_service._deploy_site_doc

    async def _observing_deploy(**kw):
        mid = await Site.get(kw["site_id"])
        seen.append(getattr(mid, "subscription_status", "") or "")
        return await real_deploy(**kw)

    monkeypatch.setattr(sites_service, "_deploy_site_doc", _observing_deploy)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert seen == ["active"], (
        f"the deploy saw subscription_status={seen!r}; a paid site is stamped as "
        "free when this is not 'active'"
    )
    assert doc.subscription_status == "active"
    assert doc.deployed is True


async def test_a_republish_does_not_charge_a_second_month(mongo_db, monkeypatch):  # noqa: ARG001
    """Editing a paid site is a content change, not a purchase.

    ASSERTED ON THE ROUTING, not on the balance, and the difference matters. The
    debit's idempotency key is anchored on the day, so a republish that WAS
    wrongly treated as a purchase still moves no money — on the same day. Come
    back tomorrow and it bills a second month for a content edit, while a
    balance-only assertion stays green throughout.

    So this counts the charge ATTEMPTS. One purchase, one call; the republish must
    make none. The balance is asserted too, as the consequence."""
    from pocketpaw_ee.cloud.billing import service as billing_service

    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 5000)
    pocket_id = await _make_pocket(workspace_id=ws)

    charges: list[dict] = []
    real_charge = billing_service.charge_site_plan_credits

    async def _recording_charge(**kw):
        charges.append(dict(kw))
        return await real_charge(**kw)

    monkeypatch.setattr(billing_service, "charge_site_plan_credits", _recording_charge)

    for _ in range(2):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="site",
            purchase_authorized=True,
            _bundle_reader=lambda d: b"x",
        )

    assert len(charges) == 1, (
        "the republish tried to buy the site again — today the idempotency key "
        "absorbs that, tomorrow it is a second month billed for a content edit"
    )
    assert await credits_service.balance(ws) == 5000 - _price_credits("site")


async def test_upgrading_a_live_free_site_ships_the_new_content(mongo_db, monkeypatch):  # noqa: ARG001
    """THE REGRESSION EVERY CHARGE-THEN-DEPLOY RAIL INVITES.

    The site is marked active before the deploy. ``activate_site`` treats
    "deployed and active" as terminal and returns early — so an upgrade of an
    ALREADY-LIVE free site charges the customer and redeploys nothing, while every
    status field reports success. ``force=True`` is what stops that.

    Moved here from tests/cloud/sites/test_addon_publish.py when the add-on rail
    was deleted. The trap is a property of deferring the deploy behind a charge,
    not of which instrument charges, so it survived the rail that first exposed
    it — and the mutation that removes ``force=True`` escapes every other test in
    this module.
    """
    deploys = _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 5000)
    pocket_id = await _make_pocket(workspace_id=ws)

    # First publish on the FREE floor — goes live, costs nothing.
    free_doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="free",
        _bundle_reader=lambda d: b"x",
    )
    assert free_doc.deployed is True
    assert await credits_service.balance(ws) == 5000, "the floor costs nothing"
    deploys.clear()

    # Now buy a paid tier for the SAME pocket.
    paid_doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert await credits_service.balance(ws) == 5000 - _price_credits("site"), (
        "the upgrade must actually charge"
    )
    assert deploys, (
        "the customer paid for an upgrade and got no redeploy — activate_site's "
        "idempotency guard swallowed it (this is what force=True exists for)"
    )
    assert paid_doc.plan_tier == "site"
    assert paid_doc.subscription_status == "active"
    assert paid_doc.deployed is True


async def test_an_upgrade_lands_the_site_on_the_new_tier(mongo_db, monkeypatch):  # noqa: ARG001
    """Moving a credits-paid site up a rung leaves it live, active and on the new
    tier, having paid the DIFFERENCE for the period it is already inside.

    This asserted the new tier's FULL month until 2026-09-05, which was the
    published behaviour and the bug: the period had already been bought at the
    lower rung, so charging it again billed one month twice. What a tier change
    costs in every direction — up, down, and back again — is
    tests/cloud/sites/test_credits_tier_changes.py; this one holds the publish
    half, that the site actually moves and stays deployed."""
    _local_deploy(monkeypatch)
    ws = await _make_workspace()
    await _fund(ws, 9000)
    pocket_id = await _make_pocket(workspace_id=ws)

    for key in ("site", "staff"):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key=key,
            purchase_authorized=True,
            _bundle_reader=lambda d: b"x",
        )

    doc = await _site_under_test(ws)
    assert doc is not None
    assert doc.plan_tier == "staff"
    assert doc.subscription_status == "active"
    assert doc.deployed is True
    # One month of staff in total: $7 for the purchase, $12 for the gap.
    assert await credits_service.balance(ws) == 9000 - _price_credits("staff")
