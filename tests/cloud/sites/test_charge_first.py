# tests/cloud/sites/test_charge_first.py — proves the charge-first per-site
# publishing contract (feat/charge-first-sites): a PAID site tier is created as
# PENDING and deployed live only after the subscription.active webhook confirms
# payment; a FREE/base tier deploys immediately. The four acceptance criteria:
#
#   1. Paid-tier publish → returns a checkout_url, the Site is deployed=False +
#      subscription_status="pending", and the deploy (generator/Cloudflare) was
#      NOT invoked.
#   2. subscription.active webhook (metadata site_id) for that pending site →
#      activate_site runs the deploy (generator/Cloudflare invoked), the Site
#      becomes deployed=True + subscription_status="active".
#   3. Free/base-tier publish → deploys immediately (generator/Cloudflare invoked),
#      deployed=True, checkout_url is None (proves no regression to the free path).
#   4. Dodo-unconfigured paid tier → falls back to immediate live publish (the user
#      is never stranded — no checkout to open, so deploy now).
#
# The generator / Cloudflare / Dodo provider are mocked exactly like the sibling
# test_site_billing.py (never touches Bun/workerd/Dodo); the webhook path signs
# with the REAL standardwebhooks library so the per-site activation routing runs
# through the verified path. Real Workspace + Pocket docs are seeded so the plan
# gate, the pocket read, and the per-site sub all run against live Beanie.
#
# Created 2026-06-24 (feat/charge-first-sites): new test module.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.billing.domain import SubscriptionCheckout
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from standardwebhooks import Webhook

SECRET = "whsec_" + base64.b64encode(b"charge-first-test-secret-32bytes!").decode()
SITE_SUB_ID = "sub_site_charge_first"
CHECKOUT_URL = "https://checkout.dodopayments.test/site/charge-first"


class _RecordingGenerator:
    """Stand-in SvelteKit generator — records build calls, never touches Bun."""

    def __init__(self):
        self.build_calls: list[dict] = []

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(dict(kw))
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _RecordingCF:
    """Stand-in Cloudflare client — records put_worker calls, never deploys."""

    def __init__(self):
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


class _RecordingBillingProvider:
    """Injected per-site subscription provider — records create_subscription and
    returns a fixed checkout url + subscription id (no Dodo SDK / network)."""

    def __init__(self, subscription_id: str = SITE_SUB_ID, checkout_url: str = CHECKOUT_URL):
        self.subscription_id = subscription_id
        self.checkout_url = checkout_url
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
            checkout_url=self.checkout_url,
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


async def _make_workspace(plan: str = "pro") -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-{plan}-{datetime.now(UTC).timestamp()}", owner="u1", plan=plan
    )
    await ws.insert()
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


def _sign(body: str, *, msg_id: str) -> dict[str, str]:
    ts = datetime.now(UTC)
    sig = Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": sig,
    }


def _site_subscription_body(*, event_type: str, workspace_id: str, site_id: str) -> str:
    """A PER-SITE Dodo subscription.* webhook body — note ``site_id`` on the
    metadata, the discriminator that routes this to the SITE / activation path."""
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
                    "plan_key": "pro",
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Criterion 1 — paid-tier publish defers the deploy + returns a checkout_url.
# ---------------------------------------------------------------------------


async def test_paid_publish_is_pending_and_returns_checkout_url(
    mongo_db, recording_bus, monkeypatch
):
    # Configure a Dodo product for the "pro" site tier so it is a chargeable PAID
    # tier (positive price + a product) → charge-first defers the deploy.
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    gen = _RecordingGenerator()
    cf = _RecordingCF()
    provider = _RecordingBillingProvider()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    # The site is PENDING — created but NOT deployed live.
    assert doc.deployed is False
    assert doc.subscription_status == "pending"
    assert doc.plan_tier == "pro"
    assert doc.subscription_id == SITE_SUB_ID

    # The deploy was DEFERRED — neither the generator nor Cloudflare ran.
    assert gen.build_calls == []
    assert cf.put_calls == []

    # The checkout link is returned: on the transient PrivateAttr AND surfaced on
    # the SiteResponse (what the router returns to the caller).
    assert doc._checkout_url == CHECKOUT_URL
    resp = sites_service._to_response(doc)
    assert resp.checkout_url == CHECKOUT_URL
    assert resp.deployed is False

    # The per-site sub was opened with the site_id on its metadata (the webhook
    # discriminator) so subscription.active can route back to this site.
    assert len(provider.calls) == 1
    assert provider.calls[0]["metadata"]["site_id"] == str(doc.id)

    # The deploy inputs were persisted so the webhook can deploy without re-reading
    # the pocket.
    persisted = await Site.find_one(Site.id == doc.id)
    assert persisted is not None
    assert persisted.deployed is False
    assert persisted.subscription_status == "pending"
    assert persisted.pending_deploy_inputs  # non-empty captured inputs

    # SitePublished is DEFERRED to activation — not emitted while pending.
    assert [e for e in recording_bus.events if e.type == "site.published"] == []


# ---------------------------------------------------------------------------
# Criterion 2 — subscription.active webhook deploys the pending site live.
# ---------------------------------------------------------------------------


async def test_active_webhook_deploys_pending_site(mongo_db, recording_bus, monkeypatch):
    # PAID tier → pending publish (deploy deferred).
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    # Force local deploy mode + fake the generator/local-server so the webhook-driven
    # activation (which can't take injected seams) needs no Bun/workerd/Cloudflare.
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    activation_gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: activation_gen)
    from pocketpaw_ee.sites import local_server

    deploy_calls: list[str] = []

    def _fake_deploy_local(site_id, project_dir):
        deploy_calls.append(site_id)
        return f"http://local/{site_id}/"

    monkeypatch.setattr(local_server, "deploy_local", _fake_deploy_local)

    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_RecordingGenerator(),  # publish path generator (must NOT run)
        _billing_provider=_RecordingBillingProvider(),
    )
    site_id = str(doc.id)
    assert doc.deployed is False  # pending

    # A verified PER-SITE subscription.active (metadata carries site_id).
    body = _site_subscription_body(
        event_type="subscription.active", workspace_id=ws, site_id=site_id
    )
    result = await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_active_charge_first_1"),
        provider=_provider(),
    )
    assert result == {"ok": True, "granted": False}

    # The deploy RAN at activation — the generator built and the (local) deploy was
    # invoked for this site.
    assert len(activation_gen.build_calls) == 1
    assert deploy_calls == [site_id]

    # The SITE is now live + active + carries a renewal date, and the captured
    # deploy inputs were cleared.
    updated = await Site.find_one(Site.id == doc.id)
    assert updated is not None
    assert updated.deployed is True
    assert updated.subscription_status == "active"
    assert updated.annual_renewal_date is not None
    assert updated.pending_deploy_inputs == {}

    # SitePublished is emitted now that the site is actually live.
    published = [e for e in recording_bus.events if e.type == "site.published"]
    assert len(published) == 1
    assert published[0].data["site_id"] == site_id


async def test_active_webhook_is_idempotent(mongo_db, monkeypatch):
    """A replayed subscription.active does not re-deploy an already-active site."""
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    activation_gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: activation_gen)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir: f"http://local/{site_id}/"
    )

    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_RecordingGenerator(),
        _billing_provider=_RecordingBillingProvider(),
    )
    site_id = str(doc.id)

    body = _site_subscription_body(
        event_type="subscription.active", workspace_id=ws, site_id=site_id
    )
    await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_active_1"), provider=_provider()
    )
    assert len(activation_gen.build_calls) == 1  # deployed once

    # A second active delivery (a replay / out-of-order) is a no-op — no re-deploy.
    body2 = _site_subscription_body(
        event_type="subscription.active", workspace_id=ws, site_id=site_id
    )
    await billing.handle_webhook(
        payload=body2.encode(), headers=_sign(body2, msg_id="evt_active_2"), provider=_provider()
    )
    assert len(activation_gen.build_calls) == 1  # still 1 — not re-deployed


async def test_activate_site_invokes_cloudflare(mongo_db, monkeypatch):
    """Direct activate_site with an injected CF client proves the Cloudflare deploy
    branch runs at activation (the CF-injected path the webhook can't reach)."""
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_RecordingGenerator(),
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
        _billing_provider=_RecordingBillingProvider(),
    )
    site_id = str(doc.id)
    assert doc.deployed is False

    gen = _RecordingGenerator()
    cf = _RecordingCF()
    activated = await sites_service.activate_site(
        workspace_id=ws,
        site_id=site_id,
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )

    # The Cloudflare put_worker ran at activation, at the stable site id.
    assert gen.build_calls and cf.put_calls == [site_id]
    assert activated.deployed is True
    assert activated.subscription_status == "active"


# ---------------------------------------------------------------------------
# Criterion 3 — free/base-tier publish deploys immediately (no regression).
# ---------------------------------------------------------------------------


async def test_free_publish_deploys_immediately(mongo_db, recording_bus):
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    gen = _RecordingGenerator()
    cf = _RecordingCF()

    # No site_plan_key → base/free tier (basic, $0, no product).
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )

    # Deployed LIVE immediately — generator + Cloudflare both ran.
    assert doc.deployed is True
    assert len(gen.build_calls) == 1
    assert cf.put_calls == [str(doc.id)]
    assert doc.plan_tier == site_plans.BASE_SITE_PLAN_KEY

    # No checkout for a free publish.
    assert getattr(doc, "_checkout_url", None) is None
    resp = sites_service._to_response(doc)
    assert resp.checkout_url is None
    assert resp.deployed is True

    # The free path emits SitePublished at publish time (the site is live now).
    assert any(e.type == "site.published" for e in recording_bus.events)


# ---------------------------------------------------------------------------
# Criterion 4 — a paid tier with NO Dodo product falls back to live publish.
# ---------------------------------------------------------------------------


async def test_paid_tier_without_dodo_product_publishes_live(mongo_db):
    """A "paid" tier (positive price) whose Dodo product is UNCONFIGURED can't open
    a checkout, so charge-first degrades to an immediate live publish — the user is
    never stranded with a pending, never-deployable site."""
    # No _dodo_product_for monkeypatch → pro has a price but NO configured product.
    assert site_plans.get_site_plan("pro").annual_price_usd > 0
    assert site_plans.get_site_plan("pro").dodo_product_id is None

    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    gen = _RecordingGenerator()
    cf = _RecordingCF()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )

    # Fell back to a LIVE publish — deployed now, no checkout, no live charge.
    assert doc.deployed is True
    assert len(gen.build_calls) == 1
    assert cf.put_calls == [str(doc.id)]
    assert doc.plan_tier == "pro"
    assert doc.subscription_id is None  # no charge opened
    assert getattr(doc, "_checkout_url", None) is None
    assert sites_service._to_response(doc).checkout_url is None
