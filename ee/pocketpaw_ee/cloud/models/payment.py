# ee/pocketpaw_ee/cloud/models/payment.py — the Payment record document
# (BC-2, the Gateway primitive).
#
# One row per captured (or in-flight) top-up payment, workspace-scoped. The
# webhook handler upserts it on a verified ``payment.succeeded`` so the credit
# grant has an auditable payment provenance alongside the BC-1 ledger entry. It
# is NOT the idempotency authority — exactly-once is enforced by BC-1's unique
# ``(workspace, idempotency_key)`` ledger index, keyed on the webhook event id —
# this doc is the human-facing payment record. The UNIQUE compound index on
# ``(gateway, gateway_event_id)`` keeps a replayed webhook from inserting a
# second row.
#
# Only ``ee.cloud.billing.service`` writes this doc (entity-isolation boundary,
# mirroring the credits/pockets entities). Registered in
# ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``billing_payments`` collection.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.
# Updated 2026-09-02 (fix/billing-reversals-and-dunning, M1 / M1a): a row is now
#   written for EVERY verified ``payment.succeeded`` that carries a routable
#   workspace, not only for the ones that granted. That is what makes a refunded
#   non-USD charge traceable at all — the currency gate used to return before the
#   write, so the population most likely to demand a refund was the one with no
#   record. Recording refused payments forces a field split the row needed
#   anyway: ``amount_credits`` is what the buyer PAID and ``credits_granted`` is
#   what we actually handed over, which is 0 on every refused path. Added
#   alongside them: ``credits_reversed`` + ``reversal_event_ids`` (the running
#   clawback total and the deliveries already applied to it) and a
#   ``(gateway, gateway_ref)`` index, because the reversal join runs through
#   ``payment_id`` and was otherwise a collection scan.

from __future__ import annotations

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class Payment(TimestampedDocument):
    """A top-up payment captured through a gateway (Dodo in v1).

    ``amount_credits`` is integer credits (1 credit == $0.01). ``status`` is
    ``succeeded`` once a verified ``payment.succeeded`` webhook landed; the row
    may be created in ``pending`` at top-up time in a later task. ``gateway`` +
    ``gateway_event_id`` are unique together so a replayed webhook never inserts
    a duplicate record.
    """

    # Tenant scope. Indexed (non-unique) — many payments per workspace.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The gateway that processed this payment, e.g. ``dodo``.
    gateway: str
    # The gateway's own payment reference (Dodo ``payment_id``), when known.
    gateway_ref: str | None = None
    # The webhook delivery id that captured this payment (Standard-Webhooks
    # ``webhook-id``). The BC-1 grant's idempotency key. Part of the unique index.
    gateway_event_id: str
    # What the buyer PAID, in the gateway's lowest denomination (== credits for
    # a USD charge, where 1 credit == $0.01). NOT what we granted — see below.
    amount_credits: int
    # What we actually GRANTED for this payment. 0 on every refused path (a
    # non-USD charge, a non-positive amount), and it is the CAP a reversal claws
    # back against: we can never take more credits than we handed out.
    #
    # None means a row written before this field existed. Those rows were ONLY
    # ever written when the grant applied, so "missing" reads as "granted
    # everything that was paid" — treating it as 0 would quietly make every
    # pre-existing payment un-reversible.
    credits_granted: int | None = None
    # Running total already clawed back by refunds / lost disputes. Capped at
    # ``credits_granted``, which is what stops a payment carrying BOTH a refund
    # and a lost dispute from being reversed twice (routine, because Verifi RDR
    # resolves disputes by refunding).
    credits_reversed: int = 0
    # The reversal webhook deliveries already applied to ``credits_reversed``.
    # The running total is incremented under a ``$ne`` filter on this list, so a
    # redelivery adds nothing even if it gets past the remaining-credits check.
    reversal_event_ids: list[str] = []  # noqa: RUF012 — Beanie field default
    # ISO currency the buyer was charged in (e.g. ``USD``), informational.
    currency: str | None = None
    # The GATEWAY's outcome, not ours: a non-USD charge is genuinely
    # ``succeeded`` with ``credits_granted == 0``.
    # ``succeeded`` | ``failed`` | ``pending``.
    status: str = "succeeded"

    class Settings:
        name = "billing_payments"
        indexes = [
            # A replayed webhook (same gateway + delivery id) collides here on
            # insert → the service treats it as already-recorded.
            IndexModel(
                [("gateway", 1), ("gateway_event_id", 1)],
                unique=True,
                name="uq_gateway_event_id",
            ),
            # The REVERSAL join. A dispute carries no metadata, so a clawback
            # finds its workspace by matching the event's ``payment_id`` against
            # ``gateway_ref``. Non-unique: the field is nullable, and one payment
            # can legitimately appear under more than one delivery id.
            IndexModel(
                [("gateway", 1), ("gateway_ref", 1)],
                name="ix_gateway_ref",
            ),
        ]
