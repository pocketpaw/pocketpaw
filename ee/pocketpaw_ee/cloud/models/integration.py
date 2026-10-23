# ee/pocketpaw_ee/cloud/models/integration.py: Beanie documents for Sites integrations.
#
# Only ``pocketpaw_ee.integrations`` reads or writes these. Registered in
# ``cloud.models.__init__`` so ``init_beanie`` wires the collections.
#
#   IntegrationConnection  a workspace's grant for one provider account (the vault row)
#   SiteIntegration        one site pocket bound to one provider (+ optional connection)
#   SiteOrder              an order a provider webhook reported for a site
#   IntegrationWebhookEvent  dedupe ledger: one row per (provider, event id)
#   NewsletterSubscriber   the built-in newsletter list, one row per (pocket, email)
#   IntegrationDemoRecord  what the dev-only ``demo`` provider's ``record`` action stores
#
# INVARIANTS A READER MUST NOT BREAK:
#   * ``IntegrationConnection.token_ciphertext`` is a ``cloud._core.crypto`` Fernet
#     token. It never goes into a DTO, an event, a log line or an error message.
#   * Bindings and newsletter rows are keyed by pocket, not by Site doc, the same as
#     ``SiteSecret``: an agent links an integration while it builds, before a publish
#     mints a Site.
#   * Money is ISO 4217 minor units (``amount_minor``), never floats.
#   * ``SiteOrder.refunds`` / ``refund_version`` / ``provider_refunded_minor`` are
#     written only by targeted updates (``integrations.refunds`` and the webhook
#     ingest), never by saving a loaded order, so concurrent writers can't drop
#     each other's refund entries.

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed
from pydantic import BaseModel, Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class IntegrationConnection(TimestampedDocument):
    """A workspace-owned OAuth grant (or account link) for one provider account."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    provider: str
    account_label: str = ""
    # The provider's stable id for the account (Stripe acct_..., a Google sub).
    account_ref: str = ""
    status: str = "active"  # active | needs_reconnect | revoked
    scopes: list[str] = Field(default_factory=list)
    # Fernet(json({access_token, refresh_token, token_type})). Never serialized out.
    token_ciphertext: str = ""
    expires_at: datetime | None = None
    created_by: str = ""
    # Refresh lease: one refresher per connection. Set atomically with
    # find_one_and_update; cleared by the holder when it finishes.
    refresh_lease_owner: str | None = None
    refresh_lease_until: datetime | None = None
    last_error: str = ""
    # Non-secret provider state (``TokenGrant.meta``), e.g. a payments account's
    # charges_enabled. Read by provider hooks for status and attention.
    meta: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "integration_connections"
        indexes = [
            IndexModel([("workspace", 1), ("provider", 1), ("account_ref", 1)]),
        ]


class SiteIntegration(TimestampedDocument):
    """One site pocket bound to one provider. Unlinking never revokes the grant."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    provider: str
    connection_id: str | None = None
    # Non-secret owner choices (which sheet, which price). Never credentials.
    settings: dict[str, Any] = Field(default_factory=dict)
    enabled_actions: list[str] = Field(default_factory=list)
    # Why the agent asked for it, shown on the chat Connect card.
    purpose: str = ""
    created_by: str = ""
    # Set when the provider's ``site_setup`` failed; the site reads setup_required
    # until a re-run succeeds. A short code, never provider output.
    setup_error: str = ""
    # A credential the provider minted for this binding (the Turnstile widget
    # secret), Fernet-encrypted with ``cloud._core.crypto``. Never in a view.
    secret_ciphertext: str = ""
    # The Turnstile sitekey baked into the site's live build ("" when the last
    # publish carried none). The gateway enforces the per-site widget only while it
    # equals the current sitekey, so a build that can't render it is never refused.
    deployed_sitekey: str = ""
    # A short per-binding lease that serialises provider-side writes which must
    # not interleave (a sheet's header row). ``bindings.claim_binding_lease``.
    lease_owner: str | None = None
    lease_until: datetime | None = None
    # Non-secret provider bookkeeping (e.g. a daily new-column budget).
    provider_state: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "site_integrations"
        indexes = [
            IndexModel([("workspace", 1), ("pocket_id", 1), ("provider", 1)], unique=True),
            IndexModel([("connection_id", 1)]),
        ]


class SiteOrderItem(BaseModel):
    ref: str = ""
    name: str = ""
    qty: int = 1
    amount_minor: int = 0


class SiteOrderRefund(BaseModel):
    """One owner refund on an order (``integrations.refunds``). ``id`` is ours and
    is the provider's idempotency key; ``client_key`` is the owner's."""

    id: str
    client_key: str = ""
    amount_minor: int = 0
    # reserved (sent, no answer yet) | pending (accepted, settling) | succeeded | failed
    status: str = "reserved"
    external_id: str = ""
    created_by: str = ""
    created_at: datetime | None = None
    error: str = ""


class SiteOrder(TimestampedDocument):
    """An order reported by a provider webhook. Amounts are ISO 4217 minor units."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    site_id: str = ""
    provider: str
    external_id: str
    status: str = "pending"  # pending | paid | refunded | failed
    amount_minor: int = 0
    currency: str = ""
    customer_email: str | None = None
    items: list[SiteOrderItem] = Field(default_factory=list)
    # The provider account the payment landed on (Stripe acct_, Razorpay acc_):
    # a refund must go through a connection to that same account.
    account_ref: str = ""
    # The provider's payment id when it differs from ``external_id`` (Razorpay
    # orders are keyed by order id; refunds need the payment id).
    payment_ref: str = ""
    # Refunds: the owner's (``refunds``) and the cumulative total the provider last
    # reported (``provider_refunded_minor``, which includes the owner's and any made
    # in the provider's dashboard). ``refund_version`` pins concurrent reservations.
    refunds: list[SiteOrderRefund] = Field(default_factory=list)
    provider_refunded_minor: int = 0
    refund_version: int = 0

    class Settings:
        name = "site_orders"
        indexes = [
            IndexModel([("provider", 1), ("external_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("pocket_id", 1), ("_id", -1)]),
        ]


class IntegrationWebhookEvent(TimestampedDocument):
    """Dedupe ledger for provider webhooks: the unique index is the idempotency key."""

    provider: str
    event_id: str
    event_type: str = ""
    workspace: str = ""
    pocket_id: str = ""

    class Settings:
        name = "integration_webhook_events"
        indexes = [
            IndexModel([("provider", 1), ("event_id", 1)], unique=True),
            # Provider retry windows are days, not months.
            IndexModel([("createdAt", 1)], expireAfterSeconds=60 * 60 * 24 * 30),
        ]


class NewsletterSubscriber(TimestampedDocument):
    """One address on a site's built-in newsletter list."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    email: str  # lowercased, validated before insert
    site_id: str = ""

    class Settings:
        name = "newsletter_subscribers"
        indexes = [
            IndexModel([("workspace", 1), ("pocket_id", 1), ("email", 1)], unique=True),
        ]


class IntegrationDemoRecord(TimestampedDocument):
    """What the ``demo`` provider's ``record`` action stored (dev and tests only)."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    args: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "integration_demo_records"
