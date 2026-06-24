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
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.

from __future__ import annotations

import logging

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import BillingTopupCaptured
from pocketpaw_ee.cloud.billing.domain import GatewayEvent
from pocketpaw_ee.cloud.billing.providers.base import IPaymentsProvider
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.payment import Payment

logger = logging.getLogger(__name__)

_GATEWAY = "dodo"
# Only this event type grants credits. Other event families (payment.failed,
# payment.processing, refunds, disputes, …) are acknowledged but never grant.
_SUCCESS_EVENT = "payment.succeeded"


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
# Webhook
# ---------------------------------------------------------------------------


async def handle_webhook(
    *,
    payload: bytes,
    headers: dict[str, str],
    provider: IPaymentsProvider | None = None,
) -> dict:
    """Verify + parse a gateway webhook, granting credits on a success event.

    Returns ``{"ok": True, "granted": <bool>}``. Raises ``ValidationError``
    (→ 400 via the cloud error handler) when the signature does not verify — the
    payload is NEVER trusted before then.

    EXACTLY-ONCE: the grant keys on the webhook ``event_id`` (BC-1's unique
    ``(workspace, idempotency_key)`` index), so a replayed delivery re-grants
    nothing. We detect the replay (same balance before/after the grant) to avoid
    a spurious capture emit, and the ``Payment`` row's own unique index keeps the
    record single too.
    """
    prov = provider or _default_provider()

    # SECURITY: signature is verified inside the provider BEFORE the body is
    # parsed. A bad signature raises ValidationError here → 400, no grant.
    event: GatewayEvent = prov.verify_and_parse_webhook(payload=payload, headers=headers)

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


__all__ = [
    "create_topup",
    "handle_webhook",
]
