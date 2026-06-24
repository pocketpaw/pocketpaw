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

from __future__ import annotations

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
    # ``active`` once a verified subscription.active landed; ``cancelled`` after
    # a subscription.cancelled. Renewals leave it ``active``.
    status: str = "active"

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
