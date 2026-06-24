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
    # Integer credits granted by this payment; 1 credit == $0.01. Always integer.
    amount_credits: int
    # ISO currency the buyer was charged in (e.g. ``USD``), informational.
    currency: str | None = None
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
        ]
