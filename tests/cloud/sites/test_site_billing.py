# tests/cloud/sites/test_site_billing.py — proves the BC-9 per-site annual plan +
# publish entitlement gate contract:
#
#   1. ``list_site_plans()`` returns tiers with an annual price + cloudflare
#      features; ``get_site_plan`` resolves a known key and rejects an unknown one.
#   2. publish_pocket with a workspace that HAS the Sites entitlement → succeeds,
#      stamps ``Site.plan_tier``, and emits ``SitePublished``.
#   3. publish_pocket with a workspace LACKING the Sites entitlement → Forbidden,
#      no Site doc created.
#   4. a PER-SITE ``subscription.active`` webhook (metadata carries a ``site_id``)
#      → updates the SITE's subscription_status to active, does NOT grant workspace
#      credits, and does NOT change Workspace.plan.
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
        self, *, plan_key, product_id, workspace_id, customer_email, metadata
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
    assert keys == ["basic", "pro", "business"]  # cheapest first

    by_key = {t.key: t for t in tiers}
    # Each tier carries an annual price (USD) + a cloudflare_features set.
    assert by_key["basic"].annual_price_usd == 0
    assert by_key["pro"].annual_price_usd > 0
    assert isinstance(by_key["pro"].cloudflare_features, frozenset)
    # Higher tiers resell more Cloudflare features (a growing ladder).
    assert by_key["basic"].cloudflare_features == frozenset()
    assert "custom_domain" in by_key["pro"].cloudflare_features
    assert by_key["pro"].cloudflare_features <= by_key["business"].cloudflare_features


def test_get_site_plan_resolves_and_rejects():
    assert site_plans.get_site_plan("pro").key == "pro"
    # An unknown / missing key resolves to None (not silently substituted).
    assert site_plans.get_site_plan("platinum") is None
    assert site_plans.get_site_plan(None) is None


# ---------------------------------------------------------------------------
# Criterion 2 — publish with the Sites entitlement succeeds, stamps plan_tier,
# emits SitePublished.
# ---------------------------------------------------------------------------


async def test_publish_with_entitlement_stamps_plan_and_emits(mongo_db, recording_bus, monkeypatch):
    # pro HAS the "sites" (Sites) feature → the gate passes.
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    # Configure a Dodo product for the "pro" site tier so the per-site sub fires.
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    fake_provider = _FakeBillingProvider()
    fake_cf = _FakeCF()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_FakeGenerator(),
        _cloudflare=fake_cf,
        _bundle_reader=lambda d: b"x",
        _billing_provider=fake_provider,
    )

    # charge-first: a PAID tier (pro) is published as PENDING — stamped with its
    # tier + subscription id, but NOT deployed live until subscription.active.
    assert doc.plan_tier == "pro"
    assert doc.subscription_id == SITE_SUB_ID
    assert doc.subscription_status == "pending"
    assert doc.deployed is False
    # The deploy was DEFERRED — the generator/Cloudflare were not invoked.
    assert fake_cf.put_calls == []

    # The per-site Dodo sub was opened with the site_id on its metadata (the
    # discriminator the renewal webhook routes on).
    assert len(fake_provider.calls) == 1
    call = fake_provider.calls[0]
    assert call["plan_key"] == "pro"
    assert call["product_id"] == "prod_site_pro"
    assert call["metadata"]["site_id"] == str(doc.id)
    assert call["metadata"]["workspace_id"] == ws

    # The persisted doc reflects the pending stamp.
    persisted = await Site.find_one(Site.id == doc.id)
    assert persisted is not None
    assert persisted.plan_tier == "pro"
    assert persisted.deployed is False

    # charge-first: SitePublished is DEFERRED to activation (the site is not live
    # yet) — it is NOT emitted at publish time for a paid pending site.
    published = [e for e in recording_bus.events if e.type == "site.published"]
    assert published == []


async def test_publish_degrades_gracefully_when_dodo_unconfigured(mongo_db, recording_bus):
    """With no Dodo product configured for the tier (v1 default), publish records
    the intended tier WITHOUT a live charge — no subscription_id, no crash."""
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        # No _billing_provider injected and no product configured → no live sub.
    )

    assert doc.plan_tier == "pro"
    assert doc.subscription_id is None
    assert doc.subscription_status == "none"
    # The publish still emits SitePublished (the site IS published, just no charge).
    assert any(e.type == "site.published" for e in recording_bus.events)


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
            site_plan_key="pro",
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


async def test_per_site_active_webhook_updates_site_not_workspace(mongo_db, monkeypatch):
    # charge-first: a paid (pro) publish creates a PENDING site; the
    # subscription.active webhook deploys it live + marks the sub active. Force
    # local deploy mode and fake the generator/local-server so the webhook-driven
    # activation needs no Bun/workerd/Cloudflare.
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setattr(sites_service, "GeneratorClient", _FakeGenerator)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir: f"http://local/{site_id}/"
    )

    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_FakeGenerator(),
        _billing_provider=_FakeBillingProvider(),
    )
    site_id = str(doc.id)
    # Published PENDING — not yet deployed.
    assert doc.deployed is False
    assert doc.subscription_status == "pending"

    # The workspace plan + credit balance BEFORE the webhook.
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    plan_before = await workspace_service.get_workspace_plan(ws)
    assert await credits.balance(ws) == 0

    # A verified PER-SITE subscription.active (metadata carries site_id).
    body = _site_subscription_body(
        event_type="subscription.active", workspace_id=ws, site_id=site_id
    )
    result = await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_site_active_1"),
        provider=_provider(),
    )

    # The per-site path never grants workspace credits.
    assert result == {"ok": True, "granted": False}
    assert await credits.balance(ws) == 0  # NO workspace credit grant

    # The workspace plan is UNCHANGED (per-site subs don't touch Workspace.plan).
    assert await workspace_service.get_workspace_plan(ws) == plan_before

    # charge-first: the SITE is now DEPLOYED live + its sub flipped to active + a
    # renewal date was set.
    updated = await Site.find_one(Site.id == doc.id)
    assert updated is not None
    assert updated.deployed is True
    assert updated.subscription_status == "active"
    assert updated.annual_renewal_date is not None


async def test_per_site_cancelled_webhook_marks_site_cancelled(mongo_db, monkeypatch):
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        _billing_provider=_FakeBillingProvider(),
    )

    cancel_body = _site_subscription_body(
        event_type="subscription.cancelled", workspace_id=ws, site_id=str(doc.id)
    )
    result = await billing.handle_webhook(
        payload=cancel_body.encode(),
        headers=_sign(cancel_body, msg_id="evt_site_cancel_1"),
        provider=_provider(),
    )

    assert result == {"ok": True, "granted": False}
    updated = await Site.find_one(Site.id == doc.id)
    assert updated.subscription_status == "cancelled"


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
                # NO site_id → workspace-plan path.
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
