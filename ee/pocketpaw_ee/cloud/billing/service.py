# ee/pocketpaw_ee/cloud/billing/service.py — the billing business logic
# (BC-2, the Gateway primitive). Sole owner of writes to the ``Payment`` Beanie
# document (entity isolation — only THIS module imports ``models.payment``).
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule, mirroring
# ``credits.service``). Public API:
#   * ``create_topup``    — build a hosted-checkout url for a credit purchase via
#                           the configured payment provider (Dodo in v1).
#   * ``handle_webhook``  — verify + parse an inbound gateway webhook, and on a
#                           ``payment.succeeded`` event grant credits EXACTLY ONCE
#                           (BC-1's idempotent ``credits.grant`` keyed on the
#                           webhook event id) and record a ``Payment`` row.
#
# EXACTLY-ONCE: the grant's idempotency is BC-1's job — ``credits.grant`` keys on
# ``(workspace, idempotency_key=event_id)`` with a unique index, so a replayed
# webhook is a no-op grant (balance unchanged, no re-emit). The ``Payment`` row's
# own unique ``(gateway, gateway_event_id)`` index is a second, independent guard
# so a replay never inserts a duplicate record. We grant FIRST (the money guard),
# then upsert the record; a duplicate record insert is swallowed.
#
# Rule 9 — ``emit(BillingTopupCaptured(...))`` fires after a grant that ACTUALLY
# applied (never on a replay no-op). Rule 10 — raise ``CloudError`` subclasses
# (ValidationError / Forbidden), never HTTPException. Rule 6 — validate at entry.
#
# SECURITY: the provider verifies the signature before this module trusts any
# field. The webhook secret / API key are never logged.
#
# CURRENCY: the 1-credit==1-cent mapping is USD-only, so ``handle_webhook`` gates
# the grant on ``currency == "USD"`` — a verified non-USD success event is acked
# but never granted (it would otherwise credit 1:1 against the wrong denomination).
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.
# Updated 2026-06-24 (security): enforce USD before granting; correct the
#   bad-signature docstring (raises ``BadRequest`` → 400, not ``ValidationError``).
# Updated 2026-06-24 (BC-7, the Subscription primitive): added ``subscribe`` (open
#   a recurring checkout for a plan tier) and extended ``handle_webhook`` to route
#   verified ``subscription.*`` deliveries. ``subscription.active`` /
#   ``subscription.renewed`` grant the tier's monthly allotment ADDITIVELY (unused
#   credits roll over) keyed on the per-renewal event id; ``subscription.active``
#   also upgrades ``Workspace.plan``; ``subscription.cancelled`` reverts the plan
#   to ``free`` WITHOUT clawing back granted credits. The Subscription doc tracks
#   the gateway lifecycle alongside the per-renewal BC-1 grants.

from __future__ import annotations

import logging

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    BillingSubscriptionGranted,
    BillingTopupCaptured,
)
from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.billing.domain import (
    GatewayEvent,
    SubscriptionEvent,
)
from pocketpaw_ee.cloud.billing.providers.base import IPaymentsProvider
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.subscription import Subscription

logger = logging.getLogger(__name__)

_GATEWAY = "dodo"
# Only this event type grants credits. Other event families (payment.failed,
# payment.processing, refunds, disputes, …) are acknowledged but never grant.
_SUCCESS_EVENT = "payment.succeeded"

# BC-7 subscription event families this service ACTS on. Each maps to a precise
# money/plan action; any other subscription.* delivery (on_hold / paused /
# failed / expired / plan_changed / updated) is acked but takes no action.
_SUB_ACTIVE = "subscription.active"
_SUB_RENEWED = "subscription.renewed"
_SUB_CANCELLED = "subscription.cancelled"
# The two grant-bearing events — each (with a fresh event id) grants the tier's
# monthly allotment additively. active ALSO upgrades the workspace plan.
_SUB_GRANT_EVENTS = frozenset({_SUB_ACTIVE, _SUB_RENEWED})


def _default_provider() -> IPaymentsProvider:
    """Build the v1 provider (Dodo) from runtime settings.

    Lazy so importing this module never constructs an SDK client. Callers may
    inject their own provider (tests pass a mock) — this is only the default.
    """
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider

    return DodoProvider.from_settings(get_settings())


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


async def create_topup(
    workspace_id: str,
    user_id: str,
    amount_credits: int,
    *,
    customer_email: str | None = None,
    provider: IPaymentsProvider | None = None,
) -> dict:
    """Create a one-time top-up and return ``{"checkout_url": ...}``.

    ``amount_credits`` is integer credits (1 credit == $0.01) and must be a
    positive integer. ``workspace_id`` is stamped onto the gateway metadata so
    the success webhook can route the grant back to the right wallet.
    """
    # Rule 6 — validate at entry.
    if (
        not isinstance(amount_credits, int)
        or isinstance(amount_credits, bool)
        or amount_credits <= 0
    ):
        raise ValidationError("billing.invalid_amount", "amount_credits must be a positive integer")
    if not workspace_id:
        raise ValidationError("billing.invalid_workspace", "workspace_id is required")

    prov = provider or _default_provider()
    checkout = await prov.create_one_time(
        amount_credits=amount_credits,
        workspace_id=workspace_id,
        customer_email=customer_email,
        metadata={"workspace_id": workspace_id, "user_id": user_id or ""},
    )
    return {"checkout_url": checkout.checkout_url}


# ---------------------------------------------------------------------------
# Subscriptions (BC-7)
# ---------------------------------------------------------------------------


def _dodo_product_for_plan(plan_key: str) -> str | None:
    """Resolve the Dodo recurring-product id for ``plan_key``, or None.

    The plan catalog (``billing.plans``) is the single source — it reads the
    ``POCKETPAW_DODO_PLAN_PRODUCTS`` mapping off settings and exposes it as the
    tier's ``dodo_product_id``. Going through the catalog (not settings directly)
    keeps the product lookup co-located with the rest of the tier's billing facts
    and degrades safely to None when nothing is configured.
    """
    tier = plan_catalog.get_plan(plan_key)
    return tier.dodo_product_id if tier is not None else None


async def subscribe(
    workspace_id: str,
    user_id: str,
    plan_key: str,
    *,
    customer_email: str | None = None,
    provider: IPaymentsProvider | None = None,
) -> dict:
    """Open a recurring subscription checkout for ``plan_key``; return its url.

    Resolves the tier's Dodo recurring-product id from the plan catalog (driven by
    the ``POCKETPAW_DODO_PLAN_PRODUCTS`` config mapping), calls the provider to
    create the subscription with ``metadata={workspace_id, plan_key}`` so the
    renewal webhook can route the per-cycle grant back to the right wallet at the
    right tier, and returns ``{"checkout_url": ...}``. Credits are NOT granted and
    the plan is NOT changed here — both land when Dodo posts a verified
    ``subscription.active`` to the public webhook.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("billing.invalid_workspace", "workspace_id is required")
    if not plan_key:
        raise ValidationError("billing.invalid_plan", "plan_key is required")

    tier = plan_catalog.get_plan(plan_key)
    if tier is None:
        # An unknown / typo'd tier — never open a checkout for a plan that isn't
        # in the catalog (the renewal grant would have no allotment to apply).
        raise ValidationError("billing.unknown_plan", f"'{plan_key}' is not a known plan tier")
    product_id = tier.dodo_product_id
    if not product_id:
        # The tier exists but no recurring product is configured for it. Fail
        # loudly here (not silently) — POCKETPAW_DODO_PLAN_PRODUCTS is unset.
        raise ValidationError(
            "billing.plan_product_unconfigured",
            f"No Dodo recurring product id is configured for plan '{plan_key}' "
            "(POCKETPAW_DODO_PLAN_PRODUCTS).",
        )

    prov = provider or _default_provider()
    checkout = await prov.create_subscription(
        plan_key=plan_key,
        product_id=product_id,
        workspace_id=workspace_id,
        customer_email=customer_email,
        metadata={"workspace_id": workspace_id, "plan_key": plan_key, "user_id": user_id or ""},
    )
    return {"checkout_url": checkout.checkout_url}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


async def handle_webhook(
    *,
    payload: bytes,
    headers: dict[str, str],
    provider: IPaymentsProvider | None = None,
) -> dict:
    """Verify + parse a gateway webhook, granting credits on a success event.

    Returns ``{"ok": True, "granted": <bool>}``. Raises ``BadRequest``
    (→ 400 via the cloud error handler) when the signature does not verify — the
    payload is NEVER trusted before then.

    Routes by event family: a one-time ``payment.*`` delivery parses to a
    ``GatewayEvent`` (top-up grant); a recurring ``subscription.*`` delivery
    parses to a ``SubscriptionEvent`` (BC-7 renewal grant + plan change).

    EXACTLY-ONCE: every grant keys on the webhook ``event_id`` (BC-1's unique
    ``(workspace, idempotency_key)`` index), so a replayed delivery re-grants
    nothing. We detect the replay (same balance before/after the grant) to avoid
    a spurious capture emit, and the ``Payment`` row's own unique index keeps the
    record single too.
    """
    prov = provider or _default_provider()

    # SECURITY: signature is verified inside the provider BEFORE the body is
    # parsed. A bad signature raises BadRequest here → 400, no grant.
    event = prov.verify_and_parse_webhook(payload=payload, headers=headers)

    # Route by the normalized event shape. A recurring subscription delivery is a
    # SubscriptionEvent (BC-7); everything else is the BC-2 one-time GatewayEvent.
    if isinstance(event, SubscriptionEvent):
        return await _handle_subscription_event(event)

    # Only a success event grants. Everything else (failed / processing / refund
    # / dispute) is acknowledged so the gateway stops retrying, but never grants.
    if event.type != _SUCCESS_EVENT:
        logger.info("billing.webhook: ignoring non-success event type=%s", event.type)
        return {"ok": True, "granted": False}

    if not event.workspace_id:
        # A success event with no workspace_id in metadata can't be routed. Ack
        # it (200) so the gateway stops retrying, but record nothing.
        logger.warning(
            "billing.webhook: payment.succeeded carried no workspace_id (event_id=%s) — ignoring",
            event.event_id,
        )
        return {"ok": True, "granted": False}
    if event.amount_credits <= 0:
        logger.warning(
            "billing.webhook: payment.succeeded had non-positive amount (event_id=%s) — ignoring",
            event.event_id,
        )
        return {"ok": True, "granted": False}
    # USD-ONLY: the 1-credit==1-cent mapping holds only for USD. A verified
    # non-USD charge would be credited 1:1 against the wrong denomination (a
    # ¥750 charge wrongly granting 750 credits == $7.50). Until the gateway
    # carries an FX-normalized amount, gate the grant on currency == USD; any
    # other currency is acked (200) so Dodo stops retrying, but grants nothing.
    if event.currency.upper() != "USD":
        # Log the event id + currency only — never the amount (PII / money).
        logger.warning(
            "billing.webhook: payment.succeeded in non-USD currency=%s (event_id=%s) — "
            "not granting (USD-only)",
            event.currency,
            event.event_id,
        )
        return {"ok": True, "granted": False}

    # Grant EXACTLY ONCE — idempotency is keyed on the webhook event id. A replay
    # collides on BC-1's unique (workspace, idempotency_key) index and no-ops.
    balance_before = await credits_service.balance(event.workspace_id)
    new_balance = await credits_service.grant(
        workspace=event.workspace_id,
        amount=event.amount_credits,
        cause="top_up",
        idempotency_key=event.event_id,
        ref={"gateway": _GATEWAY, "event_id": event.event_id},
    )
    # A genuine first grant moved the balance up by amount_credits; a replay
    # returns the unchanged current balance.
    applied = new_balance == balance_before + event.amount_credits

    # Record the payment (idempotently — the unique gateway+event_id index keeps
    # a replay from inserting a second row). The ledger entry is the money guard;
    # this row is the human-facing payment record.
    await _record_payment(event)

    if applied:
        # Rule 9 — emit only on a grant that actually applied (not a replay).
        await emit(
            BillingTopupCaptured(
                data={
                    "workspace_id": event.workspace_id,
                    "gateway": _GATEWAY,
                    "event_id": event.event_id,
                    "amount_credits": event.amount_credits,
                    "currency": event.currency,
                    "balance_after": new_balance,
                }
            )
        )
        logger.info(
            "billing.webhook: granted %d credits to workspace=%s (event_id=%s)",
            event.amount_credits,
            event.workspace_id,
            event.event_id,
        )
    else:
        logger.info(
            "billing.webhook: replay of event_id=%s — grant was a no-op, balance unchanged",
            event.event_id,
        )

    return {"ok": True, "granted": applied}


async def _record_payment(event: GatewayEvent) -> None:
    """Upsert the ``Payment`` record for a captured top-up. Idempotent.

    A replayed webhook collides on the unique ``(gateway, gateway_event_id)``
    index → ``DuplicateKeyError`` → we treat it as already recorded (no-op).
    """
    doc = Payment(
        workspace=event.workspace_id,
        gateway=_GATEWAY,
        gateway_ref=str((event.raw.get("data") or {}).get("payment_id") or "") or None,
        gateway_event_id=event.event_id,
        amount_credits=event.amount_credits,
        currency=event.currency or None,
        status="succeeded",
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        # Already recorded by a prior delivery of the same event — no-op.
        return


# ---------------------------------------------------------------------------
# Subscription webhook handling (BC-7)
# ---------------------------------------------------------------------------


async def _handle_subscription_event(event: SubscriptionEvent) -> dict:
    """Act on a VERIFIED ``subscription.*`` webhook (signature already checked).

    * ``subscription.active`` / ``subscription.renewed`` → grant the tier's
      monthly allotment ADDITIVELY (unused credits roll over) keyed on the
      per-renewal ``event_id``; ``active`` ALSO upgrades ``Workspace.plan``.
    * ``subscription.cancelled`` → revert ``Workspace.plan`` to ``free`` WITHOUT
      clawing back any granted credits.
    * any other subscription.* delivery → acked, no action.

    Returns ``{"ok": True, "granted": <bool>}`` (``granted`` is only ever True for
    a grant-bearing event that actually applied, never a replay).
    """
    # Lazy import — keeps this service free of the heavy workspace.service import
    # (Beanie) at module load, mirroring the entitlements resolver.
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    if not event.workspace_id:
        # No routable workspace on the verified metadata — ack so Dodo stops
        # retrying, but take no action (nothing to grant or upgrade).
        logger.warning(
            "billing.webhook: %s carried no workspace_id (event_id=%s) — ignoring",
            event.type,
            event.event_id,
        )
        return {"ok": True, "granted": False}

    if event.type == _SUB_CANCELLED:
        # Revert the entitlement to free. Do NOT claw back granted credits — the
        # workspace keeps the rolled-over balance it already paid for.
        ok = await workspace_service.set_workspace_plan(event.workspace_id, "free")
        await _upsert_subscription(event, status="cancelled", plan_key="free")
        logger.info(
            "billing.webhook: subscription.cancelled for workspace=%s (event_id=%s) — "
            "plan reverted to free=%s, credits NOT clawed back",
            event.workspace_id,
            event.event_id,
            ok,
        )
        return {"ok": True, "granted": False}

    if event.type not in _SUB_GRANT_EVENTS:
        # on_hold / paused / failed / expired / plan_changed / updated — acked,
        # no money/plan action in v1.
        logger.info("billing.webhook: ignoring subscription event type=%s", event.type)
        return {"ok": True, "granted": False}

    # A grant-bearing event (active | renewed). Resolve the tier so we know the
    # allotment to grant. A missing / unknown plan_key can't be granted against.
    tier = plan_catalog.get_plan(event.plan_key)
    if tier is None:
        logger.warning(
            "billing.webhook: %s carried unknown plan_key=%r (event_id=%s) — ignoring",
            event.type,
            event.plan_key,
            event.event_id,
        )
        return {"ok": True, "granted": False}
    if tier.monthly_credit_allotment <= 0:
        # A zero-allotment tier (e.g. free) grants nothing — but still record the
        # subscription / upgrade the plan on an active.
        logger.info(
            "billing.webhook: %s for zero-allotment plan=%s — no credit grant",
            event.type,
            tier.key,
        )
        applied = False
    else:
        # Grant the allotment ADDITIVELY (BC-1 grant is additive → rollover) keyed
        # on the per-renewal event id. A replay of THIS event collides on BC-1's
        # unique (workspace, idempotency_key) index and no-ops; each NEW month's
        # renewal carries a fresh event id and grants again.
        balance_before = await credits_service.balance(event.workspace_id)
        new_balance = await credits_service.grant(
            workspace=event.workspace_id,
            amount=tier.monthly_credit_allotment,
            cause="subscription_grant",
            idempotency_key=event.event_id,
            ref={
                "gateway": _GATEWAY,
                "event_id": event.event_id,
                "plan_key": tier.key,
                "subscription_id": event.subscription_id,
            },
        )
        applied = new_balance == balance_before + tier.monthly_credit_allotment

    # subscription.active upgrades the entitlement to the subscribed tier.
    if event.type == _SUB_ACTIVE:
        await workspace_service.set_workspace_plan(event.workspace_id, tier.key)

    # Track the subscription lifecycle (active for both active + renewed).
    await _upsert_subscription(event, status="active", plan_key=tier.key)

    if applied:
        # Rule 9 — emit only on a grant that actually applied (not a replay).
        await emit(
            BillingSubscriptionGranted(
                data={
                    "workspace_id": event.workspace_id,
                    "gateway": _GATEWAY,
                    "event_id": event.event_id,
                    "plan_key": tier.key,
                    "subscription_id": event.subscription_id,
                    "amount_credits": tier.monthly_credit_allotment,
                    "balance_after": new_balance,
                }
            )
        )
        logger.info(
            "billing.webhook: %s granted %d credits to workspace=%s plan=%s (event_id=%s)",
            event.type,
            tier.monthly_credit_allotment,
            event.workspace_id,
            tier.key,
            event.event_id,
        )
    else:
        logger.info(
            "billing.webhook: %s for workspace=%s (event_id=%s) — no new grant "
            "(replay or zero-allotment)",
            event.type,
            event.workspace_id,
            event.event_id,
        )

    return {"ok": True, "granted": applied}


async def _upsert_subscription(event: SubscriptionEvent, *, status: str, plan_key: str) -> None:
    """Upsert the human-facing ``Subscription`` record. Idempotent.

    Keyed on the unique ``(gateway, gateway_subscription_id)`` index: a first
    activation inserts, a renewal / cancellation of a known subscription updates
    the existing row's status / plan. A missing ``subscription_id`` (the gateway
    didn't carry one) is recorded with an empty id rather than crashing the
    webhook — the BC-1 grant is the money guard, this row is only the audit trail.
    """
    existing = await Subscription.find_one(
        Subscription.gateway == _GATEWAY,
        Subscription.gateway_subscription_id == event.subscription_id,
    )
    if existing is not None:
        existing.status = status
        existing.plan_key = plan_key
        if event.product_id:
            existing.product_id = event.product_id
        await existing.save()
        return
    doc = Subscription(
        workspace=event.workspace_id,
        gateway=_GATEWAY,
        gateway_subscription_id=event.subscription_id,
        plan_key=plan_key,
        product_id=event.product_id or None,
        status=status,
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        # A racing delivery inserted it first — re-fetch and update instead.
        existing = await Subscription.find_one(
            Subscription.gateway == _GATEWAY,
            Subscription.gateway_subscription_id == event.subscription_id,
        )
        if existing is not None:
            existing.status = status
            existing.plan_key = plan_key
            await existing.save()


__all__ = [
    "create_topup",
    "handle_webhook",
    "subscribe",
]
