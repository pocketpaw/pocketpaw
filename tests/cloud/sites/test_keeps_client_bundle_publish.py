# tests/cloud/sites/test_keeps_client_bundle_publish.py — MT-1: the pocket's
# "my client JavaScript is load-bearing" declaration survives every publish path.
# Created 2026-08-07 (feat/sites-keep-client-bundle).
#
# Edited 2026-08-08 (feat/sites-js-by-default): the declaration is now TRI-STATE,
# and ``publish_pocket`` is the one place it collapses to a bool. ``None`` — the
# author declared nothing, which covers every pocket written before MT-1 —
# resolves from ``sites_keep_client_bundle_default``, which ships True: a Paw Site
# keeps its client bundle by default. Four tests were ADDED for that: undeclared
# takes the default, the default is a setting (flip it and the same undeclared
# input inverts, which is what proves it is not a hardcoded literal), and the
# declaration beats the default in BOTH directions. One existing test was renamed
# from ``..._of_a_plain_pocket_does_not_declare`` to ``..._honours_an_explicit_
# opt_out`` with its input and assertion untouched — it always passed an explicit
# ``False`` while describing itself as "declares nothing", a distinction that did
# not exist until now. The deferred/paid tests are UNCHANGED and still pass: the
# snapshot stores the RESOLVED bool, so activation never sees the tri-state.
#
# A published Paw Site is generated with ``csr = false`` and, on ripple, has its
# emitted hydration bundle pruned after the build — so an author's own onMount /
# ``use:`` action / IntersectionObserver / WebGL code never runs. ``Pocket``
# now carries ``keeps_client_bundle``; it rides ``siteConfig.keepsClientBundle`` to
# the generator, which emits ``csr = true`` instead.
#
# THE TEST THAT MATTERS HERE IS THE DEFERRED ONE. A paid publish does not deploy:
# it snapshots the deploy inputs onto ``Site.pending_deploy_inputs`` and defers until
# the ``subscription.active`` webhook fires, and that webhook carries only
# workspace_id + site_id — it never re-reads the pocket. So any field the publish
# path reads but the snapshot omits is silently dropped, and a PAID interactive site
# would go live with its JavaScript stripped while every free-path test stayed green.
# ``test_deferred_paid_publish_preserves_the_flag`` runs the real charge-first flow
# (pending publish → signed webhook → activation) and asserts the flag reaches the
# generator at activation time.
#
# The generator / Cloudflare / Dodo provider are mocked exactly like the sibling
# test_charge_first.py (never touches Bun/workerd/Dodo); the webhook path signs with
# the REAL standardwebhooks library so activation runs through the verified route.

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

SECRET = "whsec_" + base64.b64encode(b"keeps-bundle-test-secret-32bytes!").decode()
SITE_SUB_ID = "sub_site_keeps_bundle"
CHECKOUT_URL = "https://checkout.dodopayments.test/site/keeps-bundle"


class _RecordingGenerator:
    """Stand-in generator — records the build kwargs, never touches Bun."""

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
    def __init__(self):
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
        self.calls.append({"metadata": dict(metadata)})
        return SubscriptionCheckout(checkout_url=CHECKOUT_URL, subscription_id=SITE_SUB_ID)


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id="prod_credits_sku",
        plan_products={},
    )


async def _make_workspace() -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-mt1-{datetime.now(UTC).timestamp()}", owner="u1", plan="pro"
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(*, workspace_id: str, keeps_client_bundle: bool | None) -> str:
    """A site pocket that declares its client JS is load-bearing (``True``),
    explicitly declares it is NOT (``False``), or declares nothing at all
    (``None`` — the legacy/undeclared state that publish resolves from config)."""
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name="Motion Landing",
        owner="u1",
        type="site",
        pattern="landing",
        keeps_client_bundle=keeps_client_bundle,
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


def _site_subscription_body(*, workspace_id: str, site_id: str) -> str:
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": "subscription.active",
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


def _paid_tier(monkeypatch) -> None:
    """Make the "pro" site tier chargeable so publish defers the deploy."""
    monkeypatch.setattr(
        site_plans, "_dodo_product_for", lambda key: {"pro": "prod_site_pro"}.get(key)
    )


# ---------------------------------------------------------------------------
# The free / immediate publish path.
# ---------------------------------------------------------------------------


async def test_live_publish_forwards_the_declared_flag(mongo_db, recording_bus):
    """A declaring pocket published on the free path hands the flag to the
    generator, which is what makes the emitted +layout.ts carry csr=true."""
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=True)
    gen = _RecordingGenerator()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
    )

    assert len(gen.build_calls) == 1
    assert gen.build_calls[0]["keeps_client_bundle"] is True


async def test_live_publish_honours_an_explicit_opt_out(mongo_db, recording_bus):
    """A pocket that explicitly declares ``False`` is told False, so csr stays off
    and the prune still runs.

    Renamed + re-described (feat/sites-js-by-default), NOT weakened — the
    assertion and the input are byte-identical to the MT-1 original. It was named
    ``..._of_a_plain_pocket_does_not_declare`` and called "a pocket that declares
    nothing", but it always passed an explicit ``False``. Under the old two-state
    bool those were the same value and the sloppiness was invisible; under the
    tri-state they are the two different halves of the feature, and the
    genuinely-undeclared case is now covered separately below. This test is the
    OPT-OUT path and must keep passing untouched no matter what the default is —
    that is what stops the new default being a mandate."""
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=False)
    gen = _RecordingGenerator()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
    )

    assert len(gen.build_calls) == 1
    assert gen.build_calls[0]["keeps_client_bundle"] is False


# ---------------------------------------------------------------------------
# feat/sites-js-by-default — the UNDECLARED pocket resolves from config, and an
# explicit declaration beats that config in BOTH directions.
# ---------------------------------------------------------------------------


async def _publish_undeclared(ws: str, gen: _RecordingGenerator) -> None:
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=None)
    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
    )


async def test_undeclared_pocket_takes_the_default_which_ships_js(mongo_db, recording_bus):
    """THE FEATURE. A pocket that declares NOTHING — which is every pocket
    authored before MT-1 — now publishes WITH its client bundle, because
    ``sites_keep_client_bundle_default`` ships True.

    The input here is ``None``, not ``False``: that distinction is the entire
    mechanism, and it is why the field had to become tri-state rather than just
    having its default flipped."""
    from pocketpaw.config import get_settings

    assert get_settings().sites_keep_client_bundle_default is True, (
        "the shipped default is what this test is about — if it is not True the "
        "feature is off and the assertion below is meaningless"
    )
    ws = await _make_workspace()
    gen = _RecordingGenerator()

    await _publish_undeclared(ws, gen)

    assert len(gen.build_calls) == 1
    assert gen.build_calls[0]["keeps_client_bundle"] is True


async def test_the_default_is_a_setting_not_a_hardcode(mongo_db, recording_bus, monkeypatch):
    """Turning ``sites_keep_client_bundle_default`` OFF must send an undeclared
    pocket back to the historical no-JS behaviour.

    This is what proves the flip is a SETTING and not a literal: the same
    undeclared input that produced True above produces False here with nothing
    changed but config. Without this, a hardcoded ``True`` would pass the test
    above just as well."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "sites_keep_client_bundle_default", False)
    ws = await _make_workspace()
    gen = _RecordingGenerator()

    await _publish_undeclared(ws, gen)

    assert len(gen.build_calls) == 1
    assert gen.build_calls[0]["keeps_client_bundle"] is False


async def test_an_explicit_opt_out_beats_a_default_that_says_ship_it(mongo_db, recording_bus):
    """BOTH DIRECTIONS. With the default ON, a pocket that explicitly declares
    ``False`` still gets no bundle.

    The captain asked for a default, not a mandate. This is the test that holds
    the difference: it fails the moment anyone "simplifies" the resolution at
    ``publish_pocket`` into ``declared or default``, which reads identically for
    every other input and silently strips the author's veto."""
    from pocketpaw.config import get_settings

    assert get_settings().sites_keep_client_bundle_default is True
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=False)
    gen = _RecordingGenerator()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
    )

    assert len(gen.build_calls) == 1
    assert gen.build_calls[0]["keeps_client_bundle"] is False


async def test_an_explicit_declaration_beats_a_default_that_says_dont(
    mongo_db, recording_bus, monkeypatch
):
    """BOTH DIRECTIONS, the other way. With the default OFF, a pocket that
    declares ``True`` still ships its bundle — the MT-1 guarantee survives however
    the new setting is configured."""
    from pocketpaw.config import get_settings

    monkeypatch.setattr(get_settings(), "sites_keep_client_bundle_default", False)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=True)
    gen = _RecordingGenerator()

    await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
    )

    assert len(gen.build_calls) == 1
    assert gen.build_calls[0]["keeps_client_bundle"] is True


# ---------------------------------------------------------------------------
# The charge-first DEFERRED path — the one that silently loses fields.
# ---------------------------------------------------------------------------


async def test_paid_publish_captures_the_flag_in_the_deploy_snapshot(mongo_db, monkeypatch):
    """A paid publish defers the deploy, so the flag must land in
    ``pending_deploy_inputs`` — that snapshot is all the webhook will ever see."""
    _paid_tier(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=True)
    gen = _RecordingGenerator()

    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=gen,
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
        _billing_provider=_RecordingBillingProvider(),
    )

    # Deploy really was deferred (nothing built yet) …
    assert doc.deployed is False
    assert gen.build_calls == []
    # … and the declaration is persisted for the webhook to replay.
    persisted = await Site.find_one(Site.id == doc.id)
    assert persisted is not None
    assert persisted.pending_deploy_inputs["keeps_client_bundle"] is True


async def test_deferred_paid_publish_preserves_the_flag(mongo_db, recording_bus, monkeypatch):
    """THE ONE THAT MATTERS: publish paid → pending, then let the real signed
    ``subscription.active`` webhook drive activation, and assert the generator is
    still told the site keeps its client bundle.

    The webhook carries only workspace_id + site_id and never re-reads the pocket,
    so this fails the moment the flag stops being captured in / replayed from
    ``pending_deploy_inputs`` — the failure mode no free-path test can see.
    """
    _paid_tier(monkeypatch)
    # Local deploy mode + a patched GeneratorClient, because the webhook-driven
    # activation takes no injected seams.
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    activation_gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: activation_gen)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir, **kw: f"http://local/{site_id}/"
    )

    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=True)
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_RecordingGenerator(),  # publish-path generator: must NOT run
        _billing_provider=_RecordingBillingProvider(),
    )
    site_id = str(doc.id)
    assert doc.deployed is False

    body = _site_subscription_body(workspace_id=ws, site_id=site_id)
    result = await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_active_keeps_bundle_1"),
        provider=_provider(),
    )
    assert result == {"ok": True, "granted": False}

    # The deferred deploy ran — and it still knows the site keeps its JavaScript.
    assert len(activation_gen.build_calls) == 1
    assert activation_gen.build_calls[0]["keeps_client_bundle"] is True

    updated = await Site.find_one(Site.id == doc.id)
    assert updated is not None
    assert updated.deployed is True
    assert updated.subscription_status == "active"


async def test_activation_of_a_pre_mt1_pending_site_defaults_to_false(mongo_db, monkeypatch):
    """BACK-COMPAT: a site that went pending BEFORE this field existed has no
    ``keeps_client_bundle`` key in its snapshot. Activation must read that as False
    and deploy normally, not raise."""
    _paid_tier(monkeypatch)
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    activation_gen = _RecordingGenerator()
    monkeypatch.setattr(sites_service, "GeneratorClient", lambda *a, **k: activation_gen)
    from pocketpaw_ee.sites import local_server

    monkeypatch.setattr(
        local_server, "deploy_local", lambda site_id, project_dir, **kw: f"http://local/{site_id}/"
    )

    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws, keeps_client_bundle=True)
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key="pro",
        _generator=_RecordingGenerator(),
        _billing_provider=_RecordingBillingProvider(),
    )
    site_id = str(doc.id)

    # Rewrite the snapshot to the PRE-MT-1 shape: the key simply isn't there.
    persisted = await Site.find_one(Site.id == doc.id)
    assert persisted is not None
    legacy_inputs = dict(persisted.pending_deploy_inputs)
    legacy_inputs.pop("keeps_client_bundle")
    persisted.pending_deploy_inputs = legacy_inputs
    await persisted.save()

    body = _site_subscription_body(workspace_id=ws, site_id=site_id)
    await billing.handle_webhook(
        payload=body.encode(),
        headers=_sign(body, msg_id="evt_active_keeps_bundle_legacy"),
        provider=_provider(),
    )

    # Deployed fine, with the historical (non-declaring) behaviour.
    assert len(activation_gen.build_calls) == 1
    assert activation_gen.build_calls[0]["keeps_client_bundle"] is False
