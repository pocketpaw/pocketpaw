# ee/pocketpaw_ee/integrations/refunds.py: owner refunds on site orders.
#
#   POST /sites/by-pocket/{pocket_id}/orders/{order_id}/refunds
#        {amount_minor?, idempotency_key} -> OrderView
#
# A refund is full or partial, in ISO 4217 minor units of the order's currency.
# Only the pocket owner or a workspace admin may refund, and only an order of their
# workspace's pocket (another workspace's order is a 404). The provider must have a
# ``refund_payment`` hook (Stripe, Razorpay); the order must be ``paid``.
#
# THE BOUND. What is still refundable is computed here from the stored order and
# never taken from the client: ``amount_minor`` minus what is refunded
# (``refunded_minor``: the larger of the provider's last reported cumulative total
# and the sum of the owner's accepted refunds, so a refund the provider reported
# back is never counted twice) minus refunds still in flight. A missing amount
# means all of it; more is a 422. The refund goes through the site's current
# connection, which must be to the account the payment landed on
# (``order.account_ref``), so a relinked site can't refund through another account.
#
# IDEMPOTENCY. The owner's ``idempotency_key`` is stored on the refund entry: the
# same key again returns the order as it is (re-driving the provider call when
# the first one never got an answer) and never makes a second refund. Each entry
# has our own id, which is the provider's idempotency key (Stripe
# ``Idempotency-Key``, Razorpay ``X-Refund-Idempotency`` + ``receipt``), so a
# re-driven call can't refund twice either.
#
# CONCURRENCY. An entry is reserved with one update conditional on the order's
# ``refund_version`` (re-read and re-bounded on a lost race), so two refunds at
# once can't both pass the bound. Writes here are targeted updates, never a saved
# document. A provider refusal (a 4xx it won't retry) marks the entry ``failed``
# and frees its amount; an unknown outcome (timeout, 5xx) keeps it ``reserved``
# for a replay with the same key. Either answers 502 ``provider_error``.

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.integrations import bindings, vault
from pocketpaw_ee.integrations.providers import get_provider
from pocketpaw_ee.integrations.providers.base import ProviderError, RefundRefused

logger = logging.getLogger(__name__)

COMMITTED = frozenset({"pending", "succeeded"})
_RESERVE_TRIES = 3


def _doc():
    from pocketpaw_ee.cloud.models.integration import SiteOrder

    return SiteOrder


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ accounting


def committed_minor(order: Any) -> int:
    return sum(r.amount_minor for r in order.refunds or () if r.status in COMMITTED)


def in_flight_minor(order: Any) -> int:
    return sum(r.amount_minor for r in order.refunds or () if r.status == "reserved")


def refunded_minor(order: Any) -> int:
    total = max(int(order.provider_refunded_minor or 0), committed_minor(order))
    return min(total, max(int(order.amount_minor or 0), 0))


def refundable_minor(order: Any) -> int:
    if order.status != "paid":
        return 0
    return max(0, int(order.amount_minor or 0) - refunded_minor(order) - in_flight_minor(order))


def supports_refunds(provider_id: str) -> bool:
    provider = get_provider(provider_id)
    return provider is not None and callable(getattr(provider, "refund_payment", None))


def can_refund(order: Any) -> bool:
    return supports_refunds(order.provider) and refundable_minor(order) > 0


# ------------------------------------------------------------------ the route


async def _load(workspace_id: str, pocket_id: str, order_id: str):
    from beanie import PydanticObjectId

    try:
        row = await _doc().get(PydanticObjectId(order_id))
    except Exception:  # noqa: BLE001 - a malformed id is a missing order
        row = None
    if row is None or row.workspace != workspace_id or row.pocket_id != pocket_id:
        raise NotFound("site_order", order_id)
    return row


def _not_refundable() -> ConflictError:
    return ConflictError("integrations.not_refundable", "This order can't be refunded.")


async def refund_order(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    order_id: str,
    amount_minor: int | None,
    idempotency_key: str,
    is_admin: bool = False,
):
    """Refund ``amount_minor`` (else all that is left) of one order. Returns its
    ``OrderView``."""
    from pocketpaw_ee.integrations.webhooks import order_view

    pocket = await bindings.require_edit(workspace_id, user_id, pocket_id)
    if not is_admin and pocket.owner != user_id:
        bindings._deny(user_id, workspace_id, pocket_id, "sites.order.refund", "sites.not_owner")
        raise Forbidden("sites.not_owner", "Only the site owner or an admin can refund.")
    row = await _load(workspace_id, pocket_id, order_id)

    replay = _by_key(row, idempotency_key)
    if replay is not None:
        if replay.status == "reserved":
            row = await _drive(row, replay)
        return order_view(row, full_email=True)

    provider = get_provider(row.provider)
    if provider is None or not callable(getattr(provider, "refund_payment", None)):
        raise _not_refundable()
    if refundable_minor(row) <= 0:
        raise _not_refundable()
    conn = await _connection(row)

    entry = await _reserve(row, amount_minor, idempotency_key, user_id)
    row = await _load(workspace_id, pocket_id, order_id)
    if entry.status == "reserved":
        row = await _drive(row, entry, provider=provider, conn=conn)
    return order_view(row, full_email=True)


def _by_key(row: Any, key: str):
    return next((r for r in row.refunds or () if r.client_key == key), None)


async def _connection(row: Any):
    """The site's live connection for the order's provider, to the payment's account."""
    binding = await bindings._row(row.workspace, row.pocket_id, row.provider)
    conn = await bindings.connection_for(binding) if binding is not None else None
    if conn is None or conn.status != "active":
        raise ConflictError(
            "integrations.not_connected",
            "Reconnect the payments account for this site to refund.",
        )
    if row.account_ref and conn.account_ref != row.account_ref:
        raise ConflictError(
            "integrations.refund_account_mismatch",
            "This order was paid to another account than the one connected now.",
        )
    return conn


async def _reserve(row: Any, amount_minor: int | None, key: str, user_id: str):
    """Push a ``reserved`` entry while the bound holds, pinned to ``refund_version``.
    Returns the entry (or a concurrent request's with the same key)."""
    from pocketpaw_ee.cloud.models.integration import SiteOrderRefund

    coll = _doc().get_pymongo_collection()
    for _ in range(_RESERVE_TRIES):
        replay = _by_key(row, key)
        if replay is not None:
            return replay
        if row.status != "paid":
            raise _not_refundable()
        remaining = refundable_minor(row)
        if remaining <= 0:
            raise _not_refundable()
        amount = remaining if amount_minor is None else int(amount_minor)
        if amount < 1:
            raise ValidationError("integrations.invalid_refund_amount", "Enter an amount.")
        if amount > remaining:
            raise ValidationError(
                "integrations.refund_too_large",
                "That is more than is left to refund on this order.",
                details={"refundable_minor": remaining},
            )
        entry = SiteOrderRefund(
            id=f"paw_rf_{secrets.token_hex(8)}",
            client_key=key,
            amount_minor=amount,
            status="reserved",
            created_by=user_id,
            created_at=_now(),
        )
        result = await coll.update_one(
            {"_id": row.id, "status": "paid", "refund_version": int(row.refund_version or 0)},
            {"$push": {"refunds": entry.model_dump()}, "$inc": {"refund_version": 1}},
        )
        if getattr(result, "modified_count", 0) == 1:
            return entry
        row = await _doc().get(row.id)
        if row is None:
            raise NotFound("site_order", "")
    raise ConflictError("integrations.refund_busy", "Another refund is in progress. Try again.")


async def _drive(row: Any, entry: Any, *, provider: Any = None, conn: Any = None):
    """Send ``entry`` to the provider and record the answer. Returns the fresh row."""
    provider = provider or get_provider(row.provider)
    if provider is None:
        raise _not_refundable()
    conn = conn or await _connection(row)

    async def _token(rejected: str = "") -> str:
        return await vault.get_valid_token(conn, rejected=rejected)

    try:
        status, external_id = await provider.refund_payment(
            row, entry.amount_minor, entry.id, _token
        )
    except RefundRefused as exc:
        await _settle(row, entry.id, "failed", "", error=str(exc)[:120])
        logger.info("integrations: %s refused refund on order %s", row.provider, row.id)
        raise CloudError(
            502, "provider_error", f"{provider.name} refused the refund. Nothing was refunded."
        ) from None
    except (ProviderError, vault.NotConnected) as exc:
        # Unknown outcome: keep it reserved; the same key re-drives it safely.
        logger.warning(
            "integrations: %s refund on order %s unconfirmed: %s",
            row.provider,
            row.id,
            type(exc).__name__,
        )
        raise CloudError(
            502,
            "provider_error",
            f"Couldn't confirm the refund with {provider.name}. Try again to check.",
        ) from None
    await _settle(row, entry.id, status, external_id)
    return await _doc().get(row.id)


async def _settle(row: Any, entry_id: str, status: str, external_id: str, *, error: str = ""):
    coll = _doc().get_pymongo_collection()
    status = status if status in ("pending", "succeeded", "failed") else "pending"
    await coll.update_one(
        {"_id": row.id, "refunds": {"$elemMatch": {"id": entry_id, "status": "reserved"}}},
        {
            "$set": {
                "refunds.$.status": status,
                "refunds.$.external_id": str(external_id or "")[:120],
                "refunds.$.error": error,
            },
            "$inc": {"refund_version": 1},
        },
    )
    await settle_status(row.id)


async def settle_status(order_id: Any) -> None:
    """Mark a paid order ``refunded`` once all of it is refunded."""
    fresh = await _doc().get(order_id)
    if fresh is None or fresh.status != "paid" or fresh.amount_minor <= 0:
        return
    if refunded_minor(fresh) >= fresh.amount_minor:
        await (
            _doc()
            .get_pymongo_collection()
            .update_one({"_id": fresh.id, "status": "paid"}, {"$set": {"status": "refunded"}})
        )


__all__ = [
    "can_refund",
    "committed_minor",
    "in_flight_minor",
    "refund_order",
    "refundable_minor",
    "refunded_minor",
    "settle_status",
    "supports_refunds",
]
