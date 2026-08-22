# tests/cloud/sites/test_republish_double_billing.py — proves that republishing a
# site that is ALREADY paying does not charge the customer a second time.
#
# The defect (found 2026-08-22, fix/site-republish-double-billing): the charge-first
# dispatcher branched on the TIER alone — "paid tier + configured Dodo product →
# open a checkout" — with no regard for whether this site already held an active
# subscription. So every republish of a paid site (a content edit, a re-deploy, a
# rename) ran ``create_subscription`` again:
#
#   * a SECOND Dodo subscription started billing, and the first was never cancelled;
#   * ``doc.subscription_id`` was overwritten with the new checkout SESSION id, so
#     the original subscription became unreachable — no later cancel could ever name
#     it, and it would bill until the customer disputed the charge;
#   * ``doc.subscription_status`` flipped to "pending", which every entitlement
#     resolves as unpaid — so a paying customer's custom domain started returning
#     402 and their concierge disappeared, mid-subscription;
#   * and the new content did not go live until the buyer paid AGAIN, because the
#     deploy was deferred to a ``subscription.active`` that only a second payment
#     could produce.
#
# Underneath all of that sat a quieter root cause: ``activate_site`` was never given
# the ``subscription_id`` off the verified webhook, so the site doc kept the ``cks_``
# CHECKOUT SESSION id forever. The authoritative ``sub_`` id — the only handle that
# can cancel or change a plan at the gateway — was received and thrown away. Nothing
# downstream could have been built without fixing that first.
#
# Created 2026-08-22 (fix/site-republish-double-billing): new test module.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.billing.domain import SubscriptionCheckout
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from standardwebhooks import Webhook

SECRET = "whsec_" + base64.b64encode(b"republish-billing-secret-32byte!").decode()

# The two ids are DELIBERATELY different. ``create_subscription`` returns a checkout
# SESSION id (``cks_``); the authoritative gateway subscription id (``sub_``) only
# arrives later on the verified subscription.active webhook. Tests that use one
# value for both cannot tell whether activation persisted the real one.
SESSION_ID = "cks_site_checkout_session"
GATEWAY_SUB_ID = "sub_site_real_gateway_id"
CHECKOUT_URL = "https://checkout.dodopayments.test/site/republish"


class _RecordingGenerator:
    def __init__(self):
        self.build_calls: list[dict] = []

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(dict(kw))
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _RecordingCF:
    def __init__(self):
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


class _RecordingBillingProvider:
    """Records every billing call so a test can assert on what was NOT called.

    ``create_subscription`` returns the SESSION id, mirroring the real Dodo
    checkout-session flow. ``change_plan`` returns None on success, like the SDK.
    """

    def __init__(self, *, change_plan_raises: Exception | None = None):
        self.calls: list[dict] = []
        self.change_plan_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self._change_plan_raises = change_plan_raises

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
                "return_url": return_url,
                "cancel_url": cancel_url,
            }
        )
        return SubscriptionCheckout(checkout_url=CHECKOUT_URL, subscription_id=SESSION_ID)

    async def change_plan(self, *, subscription_id, product_id, plan_key):
        if self._change_plan_raises is not None:
            raise self._change_plan_raises
        self.change_plan_calls.append(
            {
                "subscription_id": subscription_id,
                "product_id": product_id,
                "plan_key": plan_key,
            }
        )

    async def cancel_subscription(self, subscription_id: str) -> None:
        self.cancel_calls.append(subscription_id)


def _provider() -> DodoProvider:
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
        name="Acme",
        slug=f"acme-republish-{datetime.now(UTC).timestamp()}",
        owner="u1",
        plan=plan,
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
    """A per-site subscription webhook body carrying the REAL gateway sub id."""
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "subscription_id": GATEWAY_SUB_ID,
                "product_id": "prod_site_pro",
                "metadata": {
                    "workspace_id": workspace_id,
                    "site_id": site_id,
                    "plan_key": "pro",
                },
            },
        }
    )


def _configure_products(monkeypatch, mapping: dict[str, str]) -> None:
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda key: mapping.get(key))


def _fake_activation_deploy(monkeypatch):
    """The webhook-driven activation takes no injected seams, so the generator and
    the local deploy are monkeypatched instead (same approach as
    test_charge_first.py). Returns the recording generator."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: gen)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir, **kw: f"http://local/{site_id}/"
    )
    return gen


async def _publish_and_pay(ws, pocket_id, provider, monkeypatch, *, tier="pro"):
    """Publish a paid site and drive it live through the real verified webhook.

    Returns the activated Site doc — deployed, active, holding the gateway sub id.
    """
    _fake_activation_deploy(monkeypatch)
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key=tier,
        _generator=_RecordingGenerator(),
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )
    body = _site_subscription_body(
        event_type="subscription.active", workspace_id=ws, site_id=str(doc.id)
    )
    await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="msg_activate_1"),
        provider=_provider(),
    )
    return await Site.find_one(Site.id == doc.id)


# ---------------------------------------------------------------------------
# The root cause: the authoritative subscription id is received and discarded.
# ---------------------------------------------------------------------------


async def test_activation_persists_the_real_gateway_subscription_id(
    mongo_db, recording_bus, monkeypatch
):
    """``create_subscription`` returns a checkout SESSION id; the real subscription
    id arrives on the webhook. The site must end up holding the latter.

    Nothing can cancel or change a subscription it cannot name. Every other fix in
    this module depends on this one line of plumbing.
    """
    _configure_products(monkeypatch, {"pro": "prod_site_pro"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    site = await _publish_and_pay(ws, pocket_id, provider, monkeypatch)

    assert site.subscription_status == "active"
    assert site.subscription_id == GATEWAY_SUB_ID, (
        "the site kept the checkout SESSION id — the authoritative gateway "
        "subscription id from the webhook was thrown away"
    )


# ---------------------------------------------------------------------------
# The money bug: a republish must not buy the plan again.
# ---------------------------------------------------------------------------


async def test_republishing_a_paying_site_opens_no_second_subscription(
    mongo_db, recording_bus, monkeypatch
):
    """A content edit on a site that is already paying is not a purchase."""
    _configure_products(monkeypatch, {"pro": "prod_site_pro"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    await _publish_and_pay(ws, pocket_id, provider, monkeypatch)
    assert len(provider.calls) == 1, "sanity: the first publish buys the plan once"

    gen, cf = _RecordingGenerator(), _RecordingCF()
    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert len(provider.calls) == 1, (
        "republishing a site that already holds an active subscription opened a "
        "SECOND one — the customer is now billed twice for one site"
    )


async def test_republishing_a_paying_site_keeps_its_paid_status_and_goes_live(
    mongo_db, recording_bus, monkeypatch
):
    """The republish must not strip the capabilities the customer is paying for,
    and the new content must go live without a second payment."""
    _configure_products(monkeypatch, {"pro": "prod_site_pro"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    await _publish_and_pay(ws, pocket_id, provider, monkeypatch)

    gen, cf = _RecordingGenerator(), _RecordingCF()
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

    # Status and the gateway handle both survive. "pending" here would mean every
    # entitlement resolves as unpaid while the customer is still being charged.
    assert doc.subscription_status == "active"
    assert doc.subscription_id == GATEWAY_SUB_ID
    assert doc.plan_tier == "pro"

    # And the edit actually shipped, rather than waiting on a payment that will
    # never come.
    assert doc.deployed is True
    assert gen.build_calls, "the republish deferred the deploy behind a second payment"
    assert doc._checkout_url is None


async def test_republishing_a_site_that_never_paid_still_opens_a_checkout(
    mongo_db, recording_bus, monkeypatch
):
    """The guard keys on "active", not on "has a subscription_id" — and it has to.

    A site published on a paid tier but never paid for sits PENDING holding the
    checkout SESSION id of an abandoned checkout. If that counted as "already
    paying", the republish would skip billing entirely and the buyer would be left
    with a site they cannot pay for and that never goes live: the deploy waits on a
    subscription.active that no unpaid checkout will ever produce.

    Opening a fresh checkout is correct here. The abandoned session simply expires.
    """
    _configure_products(monkeypatch, {"pro": "prod_site_pro"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    async def _publish():
        return await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="pro",
            _generator=_RecordingGenerator(),
            _cloudflare=_RecordingCF(),
            _bundle_reader=lambda d: b"x",
            _billing_provider=provider,
        )

    first = await _publish()
    assert first.subscription_status == "pending"  # published, never paid

    second = await _publish()

    assert len(provider.calls) == 2, (
        "a site that never paid was treated as already paying — it can now neither pay nor go live"
    )
    assert second.subscription_status == "pending"
    assert second.deployed is False
    assert second._checkout_url == CHECKOUT_URL


# ---------------------------------------------------------------------------
# A real tier change is a plan change, not a second purchase.
# ---------------------------------------------------------------------------


async def test_moving_a_paying_site_to_another_tier_changes_the_plan(
    mongo_db, recording_bus, monkeypatch
):
    """Upgrading pro → business must move the EXISTING subscription.

    Cancel-then-create would make the customer forfeit the remainder of an annual
    term they already paid for; a second create bills them twice. The gateway's
    atomic plan change is the only option that does neither.
    """
    _configure_products(monkeypatch, {"pro": "prod_site_pro", "business": "prod_site_business"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    await _publish_and_pay(ws, pocket_id, provider, monkeypatch)

    gen, cf = _RecordingGenerator(), _RecordingCF()
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="business",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    assert len(provider.calls) == 1, "a tier change must not open a second subscription"
    assert len(provider.change_plan_calls) == 1
    call = provider.change_plan_calls[0]
    assert call["subscription_id"] == GATEWAY_SUB_ID, (
        "change_plan was handed the checkout session id, not the gateway subscription"
    )
    assert call["product_id"] == "prod_site_business"

    # The webhook acks plan_changed without acting, so the new tier has to be
    # written synchronously here or nothing ever records it.
    assert doc.plan_tier == "business"
    assert doc.subscription_status == "active"


async def test_a_failed_plan_change_leaves_the_subscription_alone(
    mongo_db, recording_bus, monkeypatch
):
    """When the gateway refuses the change, the site keeps the tier it is paying
    for — and we never 'recover' by opening a second subscription."""
    _configure_products(monkeypatch, {"pro": "prod_site_pro", "business": "prod_site_business"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)

    provider = _RecordingBillingProvider()
    await _publish_and_pay(ws, pocket_id, provider, monkeypatch)
    provider._change_plan_raises = RuntimeError("gateway said no")

    gen, cf = _RecordingGenerator(), _RecordingCF()
    with pytest.raises(Exception):
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="business",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"x",
            _billing_provider=provider,
        )

    assert len(provider.calls) == 1, (
        "a failed plan change fell back to create_subscription — that is the "
        "double-billing this fix exists to remove"
    )
    persisted = await Site.find_one(Site.id != None)  # noqa: E711
    assert persisted.plan_tier == "pro"
    assert persisted.subscription_id == GATEWAY_SUB_ID
    assert persisted.subscription_status == "active"


# ---------------------------------------------------------------------------
# The buyer has to be able to get back.
# ---------------------------------------------------------------------------


async def test_the_site_checkout_sends_the_buyer_back_to_the_app(
    mongo_db, recording_bus, monkeypatch
):
    """The frontend navigates the whole page to the checkout url. Without a
    return url the buyer pays and is left on the gateway with no way back."""
    _configure_products(monkeypatch, {"pro": "prod_site_pro"})
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    provider = _RecordingBillingProvider()

    gen, cf = _RecordingGenerator(), _RecordingCF()
    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        origin="https://app.example.com",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
        _billing_provider=provider,
    )

    call = provider.calls[0]
    assert call["return_url"] and call["return_url"].startswith("https://app.example.com")
    assert call["cancel_url"] and call["cancel_url"].startswith("https://app.example.com")
