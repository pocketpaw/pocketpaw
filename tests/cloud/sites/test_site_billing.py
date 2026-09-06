# tests/cloud/sites/test_site_billing.py — proves the BC-9 per-site annual plan +
# publish entitlement gate contract:
#
#   1. ``list_site_plans()`` returns tiers with an annual price + cloudflare
#      features; ``get_site_plan`` resolves a known key and rejects an unknown one.
#   2. publish_pocket with a workspace that HAS the Sites entitlement → succeeds,
#      stamps ``Site.plan_tier``, and emits ``SitePublished``. Asserted by
#      ``test_publish_with_no_dodo_configured_charges_the_wallet`` below, which
#      also covers what used to be criterion 2's own case: that case asserted the
#      PENDING stamp a paid publish left behind while it waited for a hosted
#      checkout, and a paid publish goes live in the request now.
#   3. publish_pocket with a workspace LACKING the Sites entitlement → Forbidden,
#      no Site doc created.
#   4. RETIRED 2026-09-05 (fix/sites-plan-credits). A per-site
#      ``subscription.active`` webhook used to drive the site's lifecycle. Paw
#      Sites left Dodo — a paid site is charged to the workspace credit balance —
#      so nothing creates such a subscription and the handler only ACKS now. The
#      half of that criterion still worth guarding, that a site-carrying delivery
#      must never reach the workspace credit-grant path, is asserted in
#      tests/cloud/billing/test_dodo_webhook.py.
#
# The Sites publish entitlement gate is the EXISTING ``require_sites_plan`` (the
# "sites" plan feature, go+), which ``publish`` runs FIRST — before any
# Site insert. go+ HAS sites; free does NOT, so the negative case is a real
# end-to-end gate test (no patching of the gate). Real Workspace + Pocket docs are
# seeded so the gate, the pocket read, and the per-site sub all run against live
# Beanie. The Dodo subscription provider is injected (a mock) so no network call
# happens; the webhook path signs with the REAL standardwebhooks library so the
# per-site routing is exercised through the verified path.
#
# Created 2026-06-24 (integration/billing-credits, BC-9): new test module.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.billing.domain import SubscriptionCheckout
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from standardwebhooks import Webhook

# A valid Standard-Webhooks secret: ``whsec_`` + base64 of a 32-byte key.
SECRET = "whsec_" + base64.b64encode(b"site-billing-test-secret-32bytes!").decode()
SITE_SUB_ID = "sub_site_dodo_xyz"


class _FakeGenerator:
    """Stand-in for the SvelteKit generator — never touches Bun/workerd."""

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    """Stand-in Cloudflare client — records put_worker calls, never deploys."""

    def __init__(self):
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


class _FakeBillingProvider:
    """Injected per-site subscription provider — records the create_subscription
    call and returns a fixed subscription id (no Dodo SDK / network)."""

    def __init__(self, subscription_id: str = SITE_SUB_ID):
        self.subscription_id = subscription_id
        self.calls: list[dict] = []

    async def create_subscription(
        self,
        *,
        plan_key,
        product_id,
        workspace_id,
        customer_email,
        metadata,
        return_url=None,
        cancel_url=None,
    ) -> SubscriptionCheckout:
        self.calls.append(
            {
                "plan_key": plan_key,
                "product_id": product_id,
                "workspace_id": workspace_id,
                "metadata": dict(metadata),
            }
        )
        return SubscriptionCheckout(
            checkout_url="https://checkout.dodopayments.test/site/abc",
            subscription_id=self.subscription_id,
        )


def _provider() -> DodoProvider:
    """A DodoProvider wired with the test webhook secret (webhook path only)."""
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id="prod_credits_sku",
        plan_products={},
    )


async def _make_workspace(plan: str) -> str:
    """Insert a real Workspace doc at ``plan`` and return its id string."""
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-{plan}-{datetime.now(UTC).timestamp()}", owner="u1", plan=plan
    )
    await ws.insert()
    return str(ws.id)


async def _fill_plan_slots(workspace_id: str, count: int = 1) -> None:
    """Occupy ``count`` of the workspace's plan-carried site slots with decoys.

    Stamped exactly as a carried site is — plan rail, staff tier, active — because
    ``plan_site_slots`` counts on ``billing_rail``, and a decoy with the wrong
    shape leaves the slot open and sends the test down the free rail while looking
    like it did the opposite."""
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


async def _make_pocket(*, workspace_id: str, owner: str = "u1") -> str:
    """Insert a real Pocket doc owned by ``owner`` and return its id string."""
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


def _sign(body: str, *, msg_id: str) -> dict[str, str]:
    ts = datetime.now(UTC)
    sig = Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": sig,
    }


def _site_subscription_body(
    *, event_type: str, workspace_id: str, site_id: str, plan_key: str = "pro"
) -> str:
    """A PER-SITE Dodo ``subscription.*`` webhook body — note ``site_id`` on the
    metadata, the discriminator that routes this to the SITE path."""
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": SITE_SUB_ID,
                "product_id": "prod_site_pro",
                "metadata": {
                    "workspace_id": workspace_id,
                    "site_id": site_id,
                    "plan_key": plan_key,
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Criterion 1 — site-plan catalog.
# ---------------------------------------------------------------------------


def test_list_site_plans_returns_tiers_with_price_and_cf_features():
    tiers = site_plans.list_site_plans()
    keys = [t.key for t in tiers]
    # Cheapest first, per-site rungs before the org flats.
    assert keys == ["free", "site", "staff", "studio", "agency"]
    # The per-site view is the subset a publish may choose from — the org flats
    # cover a whole workspace and their keys are not legal ``Site.plan_tier``
    # values.
    assert [t.key for t in site_plans.list_site_scoped_plans()] == ["free", "site", "staff"]

    by_key = {t.key: t for t in tiers}
    # Each tier carries a monthly price (USD) + a cloudflare_features set.
    assert by_key["free"].monthly_price_usd == 0
    assert by_key["site"].monthly_price_usd > 0
    assert isinstance(by_key["site"].cloudflare_features, frozenset)
    # Higher tiers resell more Cloudflare features (a growing ladder).
    assert by_key["free"].cloudflare_features == frozenset()
    assert "custom_domain" in by_key["site"].cloudflare_features
    assert by_key["site"].cloudflare_features <= by_key["staff"].cloudflare_features


def test_get_site_plan_resolves_and_rejects():
    assert site_plans.get_site_plan("site").key == "site"
    # An unknown / missing key resolves to None (not silently substituted).
    assert site_plans.get_site_plan("platinum") is None
    assert site_plans.get_site_plan(None) is None


# ---------------------------------------------------------------------------
# Criterion 2 — publish with the Sites entitlement succeeds, stamps plan_tier,
# emits SitePublished.
# ---------------------------------------------------------------------------


async def test_publish_with_no_dodo_configured_charges_the_wallet(
    mongo_db, recording_bus, monkeypatch
):
    """WITH NO DODO CONFIGURED AT ALL — the ordinary state of a self-hosted
    deployment, and until this branch the state in which a paid tier could not be
    bought.

    It used to publish live, open no subscription and record the FREE FLOOR. That
    was the least-bad option while a gateway was the only thing that could take
    money, but the customer picked a paid plan and got the plan below it with no
    error anywhere. The wallet needs no configuration, so the tier they chose is
    charged for and recorded."""
    from pocketpaw_ee.cloud.credits import service as credits_service

    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", _FakeGenerator)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir, **kw: f"http://local/{site_id}/"
    )

    ws = await _make_workspace(plan="pro")
    # Pro CARRIES three sites (2026-09-06), so the wallet is only reached once
    # those are spoken for. Filling them is what makes this an OVERFLOW purchase,
    # which is the only kind the wallet buys now.
    await _fill_plan_slots(ws, 3)
    await credits_service.grant(
        workspace=ws, amount=5000, cause="top_up", idempotency_key=f"seed-nodo-{ws}"
    )
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
        # No _billing_provider injected and no product configured. Nothing is
        # needed: the wallet is the rail.
    )

    assert doc.plan_tier == "site", "the tier the buyer chose, not the floor below it"
    assert doc.subscription_status == "active"
    assert doc.billing_rail == "credits"
    assert doc.subscription_id is None
    assert await credits_service.balance(ws) == 5000 - 700
    # The publish still emits SitePublished — the site IS live.
    assert any(e.type == "site.published" for e in recording_bus.events)


async def test_a_plan_with_a_free_slot_carries_the_site_instead_of_charging(
    mongo_db,  # noqa: ARG001
    monkeypatch,
    recording_bus,  # noqa: ARG001
):
    """The other side of the test above, and the common case now.

    The same publish on the same plan — but with a slot free, so the workspace
    subscription carries the site instead of the wallet buying it. Nothing is
    debited, no renewal is scheduled, and the buyer gets ``staff`` rather than the
    ``site`` rung they picked: the plan includes the concierge, and handing them
    the cheaper rung would withhold something they are already paying for while
    both options cost the same nothing."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", _FakeGenerator)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir, **kw: f"http://local/{site_id}/"
    )

    from pocketpaw_ee.cloud.credits import service as credits_service

    ws = await _make_workspace(plan="pro")
    await credits_service.grant(
        workspace=ws, amount=5000, cause="top_up", idempotency_key=f"seed-carry-{ws}"
    )
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="site",
        purchase_authorized=True,
        _bundle_reader=lambda d: b"x",
    )

    assert doc.billing_rail == "plan"
    assert doc.plan_tier == "staff", "the plan carries staff, not the rung asked for"
    assert doc.subscription_status == "active"
    assert doc.deployed is True
    # No renewal date: the PLAN renews, the site does not. A date here would put
    # the renewal sweep on a site the wallet never bought.
    assert doc.renewal_date is None
    assert doc.period_paid_usd == 0
    assert await credits_service.balance(ws) == 5000, "a carried site costs nothing"


async def test_publish_defaults_to_base_tier(mongo_db):
    """An omitted site_plan_key falls back to the base tier."""
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
    )
    assert doc.plan_tier == site_plans.BASE_SITE_PLAN_KEY


# ---------------------------------------------------------------------------
# Criterion 3 — publish without the Sites entitlement is Forbidden, no Site.
# ---------------------------------------------------------------------------


async def test_publish_without_entitlement_is_forbidden_and_creates_no_site(mongo_db):
    from pocketpaw_ee.cloud._core.errors import Forbidden

    # free does NOT have the "sites" (Sites) feature → the gate denies.
    ws = await _make_workspace(plan="free")
    pocket_id = await _make_pocket(workspace_id=ws)

    with pytest.raises(Forbidden) as exc:
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="site",
            purchase_authorized=True,
            _generator=_FakeGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
        )
    assert exc.value.code == "plan.feature_denied"

    # No Site doc was created for the workspace.
    sites = await Site.find(Site.workspace == ws).to_list()
    assert sites == []


# ---------------------------------------------------------------------------
# Criterion 4 — a per-site subscription.active webhook updates the SITE only.
# ---------------------------------------------------------------------------


async def test_workspace_plan_sub_still_routes_to_workspace_path(mongo_db):
    """A subscription delivery WITHOUT a site_id is the unchanged BC-7 workspace
    path — it grants workspace credits / changes the plan, proving BC-9 routing
    didn't break the workspace-plan sub."""
    from pocketpaw_ee.cloud.billing import plans

    ws = await _make_workspace(plan="free")
    allotment = plans.get_plan("pro").monthly_credit_allotment

    body = json.dumps(
        {
            "business_id": "biz_1",
            "type": "subscription.active",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": "sub_ws_plan",
                "product_id": "prod_pro_recurring",
                # NO site_id → workspace-plan path. ``plan_key`` here is a
                # WORKSPACE tier (billing.plans: free/go/pro/pro_max/enterprise),
                # not a site tier — the two catalogs share a field name on the
                # webhook metadata and nothing else.
                "metadata": {"workspace_id": ws, "plan_key": "pro"},
            },
        }
    )
    result = await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_ws_plan_active"),
        provider=_provider(),
    )
    assert result == {"ok": True, "granted": True}  # workspace path DID grant
    assert await credits.balance(ws) == allotment
