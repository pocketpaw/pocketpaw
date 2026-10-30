# tests/cloud/sites/test_billing_lifecycle.py — proves the three billing-lifecycle
# hardening items shipped on feat/billing-lifecycle (review-flagged loose ends on
# the charge-first backend):
#
#   (A) deploy-input cap — a pending publish whose serialized deploy inputs exceed
#       _MAX_PENDING_DEPLOY_INPUT_BYTES raises ValidationError and persists NO Site
#       doc (a pathological ripple_spec/source map can't bloat the Site doc).
#   (B) checkout-before-persist — RETIRED 2026-09-05 (fix/sites-plan-credits).
#       It guaranteed an ordering between opening a hosted Dodo checkout and
#       writing the pending Site doc, so a checkout failure could not leave an
#       orphan row. Paw Sites left Dodo: no checkout is opened, so there is no
#       ordering to guarantee and nothing to orphan. A refused CREDIT debit leaves
#       the row pending and undeployed with nothing billed, which is the same
#       recovery the ordering was protecting and needs no ordering at all. The
#       remaining cases here are (A) and (C).
#   (C) pending-reconciliation sweeper — sweep_pending_sites() finds PAID sites stuck
#       in subscription_status == "pending" (not deployed) older than the threshold
#       and returns / logs them at WARNING for operator visibility (a lost/delayed
#       subscription.active webhook). A recently-pending or an active/deployed site
#       is NOT flagged. VISIBILITY ONLY — no auto-deploy, no auto-cancel.
#
# The generator / Cloudflare / Dodo provider are mocked exactly like the sibling
# test_charge_first.py (never touches Bun/workerd/Dodo). Real Workspace + Pocket
# docs are seeded so the plan gate, the pocket read, and the per-site sub run
# against live Beanie.
#
# Created 2026-06-24 (feat/billing-lifecycle): new test module.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing.domain import SubscriptionCheckout
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.pending_sweeper import sweep_pending_sites

SITE_SUB_ID = "sub_site_lifecycle"
CHECKOUT_URL = "https://checkout.dodopayments.test/site/lifecycle"


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
        return SubscriptionCheckout(
            checkout_url=self.checkout_url,
            subscription_id=self.subscription_id,
        )


async def _make_workspace(plan: str = "pro") -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-{plan}-{datetime.now(UTC).timestamp()}", owner="u1", plan=plan
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(*, workspace_id: str, owner: str = "u1", ripple_spec=None) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name="My Landing",
        owner=owner,
        type="site",
        pattern="landing",
        rippleSpec=ripple_spec or {},
    )
    await doc.insert()
    return str(doc.id)


# ---------------------------------------------------------------------------
# (A) deploy-input cap — oversized pending inputs raise + persist nothing.
# ---------------------------------------------------------------------------


async def test_oversized_deploy_inputs_raise_and_persist_nothing(mongo_db, monkeypatch):
    # A rippleSpec that serializes well past the 4MB cap.
    big_blob = "x" * (sites_service._MAX_PENDING_DEPLOY_INPUT_BYTES + 1)
    ripple_spec = {"bloat": big_blob}

    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws, ripple_spec=ripple_spec)

    with pytest.raises(ValidationError) as ei:
        await sites_service.publish_pocket(
            workspace_id=ws,
            user_id="u1",
            pocket_id=pocket_id,
            site_plan_key="site",
            purchase_authorized=True,
            _generator=_RecordingGenerator(),
            _cloudflare=_RecordingCF(),
            _bundle_reader=lambda d: b"x",
        )
    assert ei.value.code == "sites.deploy_inputs_too_large"

    # NO Site doc was persisted for the pocket (the cap fires before any write).
    assert await Site.find(Site.pocket_id == pocket_id).to_list() == []
    # And nothing was billed: the cap fires before any charge, so a wallet that
    # was never funded still reads zero rather than negative.
    from pocketpaw_ee.cloud.credits import service as credits_service

    assert await credits_service.balance(ws) == 0


# ---------------------------------------------------------------------------
# (B) checkout-before-persist — doc lands with subscription_id; a checkout
#     failure leaves NO orphan pending doc; free/paid happy paths still work.
# ---------------------------------------------------------------------------


async def test_free_publish_still_works(mongo_db):
    ws = await _make_workspace(plan="pro")
    pocket_id = await _make_pocket(workspace_id=ws)

    gen = _RecordingGenerator()
    cf = _RecordingCF()
    doc = await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    assert doc.deployed is True
    assert cf.put_calls == [str(doc.id)]
    assert getattr(doc, "_checkout_url", None) is None


# ---------------------------------------------------------------------------
# (C) pending-reconciliation sweeper — surfaces stuck-pending paid sites.
# ---------------------------------------------------------------------------


async def _insert_site(
    *,
    workspace: str,
    pocket_id: str,
    subscription_status: str,
    deployed: bool,
    age_hours: float,
) -> Site:
    """Insert a Site doc and backdate its createdAt to age_hours ago."""
    doc = Site(
        workspace=workspace,
        pocket_id=pocket_id,
        owner="u1",
        name=pocket_id,
        deployed=deployed,
        subscription_status=subscription_status,
        plan_tier="site",
    )
    await doc.insert()
    # Backdate createdAt so the staleness threshold can be exercised.
    doc.createdAt = datetime.now(UTC) - timedelta(hours=age_hours)
    await doc.save()
    return doc


async def test_sweeper_flags_only_stuck_pending_sites(mongo_db, monkeypatch, caplog):
    monkeypatch.setenv("POCKETPAW_SITE_PENDING_ALERT_HOURS", "24")

    ws = "w-sweep"
    # Stuck: pending + not deployed + 48h old → flagged.
    stuck = await _insert_site(
        workspace=ws,
        pocket_id="p-stuck",
        subscription_status="pending",
        deployed=False,
        age_hours=48,
    )
    # Recent: pending + not deployed but only 1h old → NOT flagged (below threshold).
    await _insert_site(
        workspace=ws,
        pocket_id="p-recent",
        subscription_status="pending",
        deployed=False,
        age_hours=1,
    )
    # Active + deployed (the happy path completed) → NOT flagged.
    await _insert_site(
        workspace=ws,
        pocket_id="p-active",
        subscription_status="active",
        deployed=True,
        age_hours=48,
    )

    with caplog.at_level(logging.WARNING):
        flagged = await sweep_pending_sites()

    flagged_ids = {str(d.id) for d in flagged}
    assert flagged_ids == {str(stuck.id)}
    # It logs at WARNING for operator visibility, naming the stuck site.
    assert any(
        record.levelno == logging.WARNING and str(stuck.id) in record.getMessage()
        for record in caplog.records
    )


async def test_sweeper_does_not_mutate_state(mongo_db, monkeypatch):
    """Visibility only — the sweeper never auto-deploys or auto-cancels."""
    monkeypatch.setenv("POCKETPAW_SITE_PENDING_ALERT_HOURS", "24")
    ws = "w-no-mutate"
    stuck = await _insert_site(
        workspace=ws,
        pocket_id="p-stuck",
        subscription_status="pending",
        deployed=False,
        age_hours=48,
    )

    await sweep_pending_sites()

    after = await Site.find_one(Site.id == stuck.id)
    assert after is not None
    assert after.subscription_status == "pending"  # unchanged
    assert after.deployed is False  # not auto-deployed
