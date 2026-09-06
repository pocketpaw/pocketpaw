# ee/pocketpaw_ee/cloud/models/subscription.py — the Subscription record document
# (BC-7, the Subscription primitive).
#
# One row per workspace recurring subscription, workspace-scoped. The webhook
# handler upserts it on a verified ``subscription.active`` (first activation) and
# tracks its status across renewals / cancellation, so a workspace's billing
# subscription has an auditable record alongside the per-renewal BC-1 grants. It
# is NOT the idempotency authority for grants — exactly-once on the credit grant
# is enforced by BC-1's unique ``(workspace, idempotency_key)`` ledger index, keyed
# on the per-renewal webhook event id. This doc is the human-facing subscription
# state. The UNIQUE index on ``(gateway, gateway_subscription_id)`` keeps one row
# per gateway subscription (a replayed activation never inserts a second).
#
# Only ``ee.cloud.billing.service`` writes this doc (entity-isolation boundary,
# mirroring the credits / payment entities). Registered in
# ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``billing_subscriptions`` collection.
#
# Created 2026-06-24 (integration/billing-credits, BC-7): new entity.
# Updated 2026-09-02 (fix/billing-reversals-and-dunning, M5): ``status`` gained
#   ``on_hold`` (the card is failing and Dodo is retrying) and ``expired``
#   (terminal at the gateway), and two dunning timestamps arrived with them —
#   ``grace_until``, the deadline after which the plan is revoked, and
#   ``suspended_at``, the stamp that records the sweep already did so. The pair
#   is deliberate: a suspended subscription keeps ``status == "on_hold"``
#   because suspension is OURS (it revokes the entitlements we grant) while the
#   subscription is still alive at Dodo and can still recover, so it must stay
#   billable or a re-subscribe opens a second one alongside it.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class Subscription(TimestampedDocument):
    """A workspace's recurring plan subscription, captured through a gateway.

    ``plan_key`` is the tier the workspace subscribed to (matches
    ``Workspace.plan`` and the ``PLAN_FEATURES`` key). ``status`` tracks the
    gateway lifecycle: ``active`` once a verified ``subscription.active`` landed,
    ``cancelled`` after a ``subscription.cancelled``. ``gateway`` +
    ``gateway_subscription_id`` are unique together so one row tracks one gateway
    subscription across its renewals.
    """

    # Tenant scope. Indexed (non-unique) — a workspace may have historical rows.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The gateway that processed this subscription, e.g. ``dodo``.
    gateway: str
    # The gateway's own subscription id (Dodo ``subscription_id``). Part of the
    # unique index so a replayed activation never inserts a duplicate row.
    gateway_subscription_id: str
    # The plan tier this subscription grants (team | business | enterprise).
    plan_key: str
    # The gateway recurring product id this subscription is for, when known.
    product_id: str | None = None
    # The gateway lifecycle:
    #   ``active``    — paying; set by a verified subscription.active/.renewed
    #   ``on_hold``   — a payment failed and Dodo is retrying (M5). Still
    #                   BILLABLE: opening a second subscription alongside it is
    #                   a double charge.
    #   ``expired``   — terminal at the gateway (subscription.expired/.failed)
    #   ``cancelled`` — terminal, after a verified subscription.cancelled
    status: str = "active"
    # When the plan is revoked if the card is still failing (M5). Stamped on the
    # transition INTO ``on_hold`` and cleared by a successful renewal. A
    # redelivered on_hold must not restamp it, or an at-least-once gateway hands
    # a non-paying workspace a grace period that never expires.
    grace_until: datetime | None = None
    # When the grace sweep actually revoked the plan. Distinct from
    # ``grace_until`` because it is what makes the sweep idempotent: an
    # already-suspended row is skipped rather than re-revoked every five minutes.
    suspended_at: datetime | None = None

    class Settings:
        name = "billing_subscriptions"
        indexes = [
            # One row per gateway subscription — a replayed activation (same
            # gateway + subscription id) collides here on insert → the service
            # updates the existing row instead of inserting a duplicate.
            IndexModel(
                [("gateway", 1), ("gateway_subscription_id", 1)],
                unique=True,
                name="uq_gateway_subscription_id",
            ),
        ]
