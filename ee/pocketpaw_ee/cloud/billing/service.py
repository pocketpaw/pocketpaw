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
# Updated 2026-06-24 (B1 review fix): the capture-event emit (BillingTopupCaptured
#   / BillingSubscriptionGranted) is now gated on ``credits.grant``'s ``created``
#   flag, NOT the prior balance-delta heuristic (``new_balance == before +
#   amount``). Under concurrency a racing grant could shift the balance so a
#   genuine first grant looked like a replay (emit wrongly suppressed) or a replay
#   looked genuine (spurious emit). ``grant`` now returns a ``GrantResult``; the
#   created-flag is the authoritative "this grant actually applied" signal.
# Updated 2026-06-24 (BC-9, per-site annual plan): the subscription webhook
#   dispatch now FORKS on a ``site_id`` in the verified event metadata. A delivery
#   WITH a ``site_id`` is a PER-SITE annual sub (each published site has its own
#   plan) and routes to ``_handle_site_subscription_event``, which updates the
#   SITE's ``subscription_status`` / ``annual_renewal_date`` (active/renewed →
#   active; cancelled → cancelled) via the sites service — it NEVER grants
#   workspace credits and NEVER changes ``Workspace.plan``. A delivery WITHOUT a
#   ``site_id`` is the unchanged BC-7 workspace-plan path. Same verified-signature
#   + idempotency guards apply (the per-site path is reached only AFTER
#   ``verify_and_parse_webhook`` succeeds).
# Updated 2026-06-24 (feat/charge-first-sites): the per-site ``subscription.active``
#   delivery now drives CHARGE-FIRST ACTIVATION. A paid-tier site is published as
#   PENDING (created but NOT deployed live); on the verified ``subscription.active``
#   the per-site path calls ``sites_service.activate_site``, which runs the deferred
#   deploy (generate + smoke-gate + Cloudflare/local deploy) and marks the sub
#   active — so a paid site goes live ONLY after payment confirms. ``activate_site``
#   is idempotent (an already-active/deployed site is a no-op), so a replayed
#   ``active`` does not re-deploy. ``renewed`` just refreshes the renewal date (the
#   site is already live), ``cancelled`` marks the site cancelled WITHOUT undeploying
#   it in v1.
# Updated 2026-06-28 (fix/billing-checkout-sessions): ``subscribe`` now takes an
#   ``origin`` (the buyer's app origin, read off the /subscribe route's Origin /
#   Referer header) and threads it through ``_checkout_return_urls`` into the
#   provider's ``return_url`` / ``cancel_url`` so Dodo returns the buyer to
#   ``{origin}/settings/billing?checkout=success|cancel`` after pay/cancel (the
#   prior payment-link checkout had nowhere to send the buyer). Falls back to the
#   ``dodo_checkout_return_base`` config when no origin is present; omits the
#   redirect entirely if both are absent. The webhook grant + idempotency are
#   unchanged — they key on the event body's subscription_id + metadata + event_id.
# Updated 2026-07-08 (feat/billing-cancel-downgrade): two money-management fixes.
#   (A) ``cancel`` — wire the previously dead-coded provider ``cancel_subscription``
#   end to end: load the workspace's ACTIVE Subscription row (the ``(workspace)``
#   index is NON-unique, so select ``status == "active"`` specifically, never a
#   naive first-match that could pick a stale cancelled row) and tell the gateway to
#   stop billing it; 402 ``billing.no_active_subscription`` when there is none. The
#   entitlement revert stays REACTIVE on the ``subscription.cancelled`` webhook — this
#   only adds the initiation side (no duplicate plan-revert). (B) ``subscribe`` no
#   longer double-subscribes: when a workspace ALREADY has an active subscription a
#   plain ``create_subscription`` opened a SECOND parallel Dodo subscription (double
#   billing). It now runs a GUARDED cancel-then-create — open the NEW checkout FIRST
#   (a create failure leaves the current sub untouched, no coverage gap), THEN cancel
#   the OLD subscription at the gateway (billing stops synchronously, so the two are
#   never both billing). Webhook ``event_id`` idempotency is untouched. See the
#   ``subscribe`` body for the native-``change_plan`` follow-up note.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import NoActiveSubscription, ValidationError
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


def _checkout_return_urls(origin: str | None) -> tuple[str | None, str | None]:
    """Build (return_url, cancel_url) for the subscription checkout, or (None, None).

    After paying on Dodo the buyer must land back in the app. The base is the
    buyer's ``origin`` (the route reads it from the Origin / Referer header); when
    that is absent we fall back to the ``dodo_checkout_return_base`` config. If
    BOTH are empty we return (None, None) and the provider omits return_url rather
    than crash — a checkout with no redirect still works, the buyer just isn't
    auto-returned.
    """
    base = (origin or "").strip()
    if not base:
        from pocketpaw.config import get_settings

        base = (getattr(get_settings(), "dodo_checkout_return_base", "") or "").strip()
    if not base:
        return None, None
    base = base.rstrip("/")
    return (
        f"{base}/settings/billing?checkout=success",
        f"{base}/settings/billing?checkout=cancel",
    )


async def _active_subscription(workspace_id: str) -> Subscription | None:
    """The workspace's currently-ACTIVE gateway subscription, or None.

    The ``Subscription`` ``(workspace)`` index is NON-UNIQUE — a workspace
    accumulates historical rows over its lifetime (a prior tier it switched off, an
    earlier subscription that was cancelled). So NEVER take a naive first-match:
    filter on ``status == "active"`` and, if more than one somehow qualifies, take
    the most-recent (``-createdAt``) for a deterministic pick. Returns None when the
    workspace has no active subscription (only historical / cancelled rows, or never
    subscribed).
    """
    return (
        await Subscription.find(
            Subscription.workspace == workspace_id,
            Subscription.status == "active",
        )
        .sort("-createdAt")
        .first_or_none()
    )


async def subscribe(
    workspace_id: str,
    user_id: str,
    plan_key: str,
    *,
    origin: str | None = None,
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

    ``origin`` is the buyer's app origin (the route reads it from the Origin /
    Referer header). It builds the return_url / cancel_url Dodo sends the buyer
    back to after pay / cancel, so the buyer is NOT stranded on the gateway. When
    absent it falls back to the ``dodo_checkout_return_base`` config; if both are
    empty the redirect is simply omitted.

    DOWNGRADE / SWITCH: if the workspace ALREADY has an active subscription this
    does NOT open a second parallel one (which would double-bill). It runs a guarded
    cancel-then-create — see the body — so a plan switch replaces the old
    subscription instead of stacking on top of it.
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

    return_url, cancel_url = _checkout_return_urls(origin)
    prov = provider or _default_provider()

    # DOWNGRADE / SWITCH GUARD (fix double-subscribe). If the workspace ALREADY has
    # an active gateway subscription, a plain create would open a SECOND parallel
    # Dodo subscription and double-bill (the bug). Detect the active sub up front,
    # then run a GUARDED cancel-then-create:
    #   1. open the NEW checkout FIRST — if it raises we surface the error here and
    #      the current subscription is untouched (no coverage gap, no orphaned
    #      cancel; the tenant keeps billing the plan they had).
    #   2. ONLY after the new checkout is open, cancel the OLD subscription at the
    #      gateway. Dodo stops billing the old one synchronously, so the two are
    #      never both billing; the plan mutations still land reactively on the
    #      webhooks (the new ``subscription.active`` upgrades, the old
    #      ``subscription.cancelled`` reverts) exactly as a fresh subscribe.
    # Webhook idempotency is untouched — each grant still keys on its own event_id
    # (BC-1's unique index), so a replayed active/renewed after a switch re-grants
    # nothing.
    #
    # FOLLOW-UP (native change_plan): Dodo's SDK exposes an ATOMIC
    # ``subscriptions.change_plan`` (proration, no re-checkout, no drop-to-free
    # window) that would remove the residual "buyer abandons the new checkout after
    # the old is cancelled" gap. Wiring it is out of scope for this bug fix — it
    # needs a new provider-port method, a ``subscription.plan_changed`` webhook
    # handler (grant + plan + audit; the service acts on active/renewed/cancelled
    # only today), and a /subscribe response-contract change (change_plan returns no
    # checkout url). Tracked as a billing plan-change follow-up.
    existing = await _active_subscription(workspace_id)

    checkout = await prov.create_subscription(
        plan_key=plan_key,
        product_id=product_id,
        workspace_id=workspace_id,
        customer_email=customer_email,
        metadata={"workspace_id": workspace_id, "plan_key": plan_key, "user_id": user_id or ""},
        return_url=return_url,
        cancel_url=cancel_url,
    )

    if existing is not None and existing.gateway_subscription_id:
        # Cancel the prior subscription now that the new checkout is open. If THIS
        # raises, the caller gets an error and the OLD sub keeps billing (never a
        # silent double-charge); the just-opened checkout session simply expires
        # unused (no new subscription exists until the buyer pays it).
        await prov.cancel_subscription(existing.gateway_subscription_id)
        logger.info(
            "billing.subscribe: workspace=%s switching plans — opened new checkout "
            "for plan=%s and cancelled prior subscription=%s",
            workspace_id,
            plan_key,
            existing.gateway_subscription_id,
        )

    return {"checkout_url": checkout.checkout_url}


async def cancel(
    workspace_id: str,
    *,
    provider: IPaymentsProvider | None = None,
) -> dict:
    """Cancel the workspace's ACTIVE recurring subscription at the gateway.

    Loads the workspace's currently-active ``Subscription`` row and tells the
    gateway to stop billing it (``provider.cancel_subscription``). Returns
    ``{"ok": True}``.

    The entitlement revert (``Workspace.plan`` -> free) and the Subscription-row
    status flip are NOT done here — they land REACTIVELY on the verified
    ``subscription.cancelled`` webhook (mirroring how ``subscribe`` defers the
    upgrade to the ``subscription.active`` webhook; the webhook handler is the sole
    writer of that plan mutation, so cancelling here would duplicate it).

    Raises 402 ``billing.no_active_subscription`` when the workspace has no active
    subscription — only historical / already-cancelled rows, or never subscribed.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("billing.invalid_workspace", "workspace_id is required")

    active = await _active_subscription(workspace_id)
    if active is None or not active.gateway_subscription_id:
        # An active row with a gateway id is required. A stale cancelled row or no
        # subscription at all is a 402 (not a silent success, and not a naive
        # first-match against a historical row).
        raise NoActiveSubscription()

    prov = provider or _default_provider()
    await prov.cancel_subscription(active.gateway_subscription_id)
    logger.info(
        "billing.cancel: requested gateway cancel for workspace=%s subscription=%s "
        "(plan revert lands reactively on the subscription.cancelled webhook)",
        workspace_id,
        active.gateway_subscription_id,
    )
    return {"ok": True}


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
    nothing. We detect the replay via ``credits.grant``'s ``created`` flag
    (False on a duplicate-key replay) to avoid a spurious capture emit, and the
    ``Payment`` row's own unique index keeps the record single too.
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
    result = await credits_service.grant(
        workspace=event.workspace_id,
        amount=event.amount_credits,
        cause="top_up",
        idempotency_key=event.event_id,
        ref={"gateway": _GATEWAY, "event_id": event.event_id},
    )
    new_balance = result.balance
    # A genuine first grant reports ``created=True``; a replay reports False. We
    # gate the capture emit on this authoritative flag, NOT a balance delta — a
    # delta heuristic mis-fires under concurrency (a racing grant could mask a
    # genuine grant as a replay or vice versa).
    applied = result.created

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
    # BC-9: a PER-SITE annual sub carries a ``site_id`` on its metadata; a
    # workspace-plan sub does not. Route a per-site delivery to the SITE (update
    # its subscription_status / renewal date) instead of the workspace path (which
    # grants credits / changes Workspace.plan). The site_id is the sole
    # discriminator — everything below stays the unchanged BC-7 workspace path.
    if event.site_id:
        return await _handle_site_subscription_event(event)

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
        result = await credits_service.grant(
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
        new_balance = result.balance
        # Gate the grant emit on the authoritative created-flag (True on a real
        # first grant, False on a duplicate-key replay), NOT a balance delta — the
        # delta heuristic races under concurrency.
        applied = result.created

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


async def _handle_site_subscription_event(event: SubscriptionEvent) -> dict:
    """Act on a VERIFIED PER-SITE ``subscription.*`` webhook (BC-9 + charge-first).

    A per-site annual sub (each published site has its OWN recurring plan) is
    distinguished from a workspace-plan sub by the ``site_id`` on its metadata.
    This path updates the SITE's lifecycle ONLY — it NEVER grants workspace
    credits and NEVER changes ``Workspace.plan`` (the two grant/plan side effects
    of the BC-7 workspace path). All site writes are delegated to the sites service
    (entity isolation — billing never imports the Site model):

      * ``subscription.active`` → CHARGE-FIRST ACTIVATION. A paid-tier site was
        published as PENDING (created but NOT deployed live) and is deployed + goes
        live only now that payment is confirmed: ``sites_service.activate_site``
        runs the deferred deploy (generate + smoke-gate + Cloudflare/local deploy)
        and marks the sub active. It is idempotent — an already-active/deployed
        site is a no-op, so a replayed delivery (or a renewal that arrives as
        ``active``) does not re-deploy.
      * ``subscription.renewed`` → the site is already live; just refresh the
        renewal date (one year out) via ``mark_site_subscription`` — no re-deploy.
      * ``subscription.cancelled`` → mark the site sub ``cancelled``. The live site
        is NOT undeployed in v1 (a cancelled annual plan keeps serving until the
        paid period would lapse; an undeploy/teardown is a follow-up).
      * any other subscription.* delivery → acked, no action.

    Returns ``{"ok": True, "granted": False}`` always — a per-site sub never
    moves the workspace credit wallet, so ``granted`` is never True here.
    """
    # Lazy import — keeps billing free of an eager sites-service import at module
    # load, mirroring the workspace-service lazy import in the BC-7 path.
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.sites import service as sites_service

    try:
        if event.type == _SUB_ACTIVE:
            # CHARGE-FIRST: payment confirmed → deploy the pending site live and
            # mark the sub active. Idempotent for at-least-once delivery.
            await sites_service.activate_site(
                workspace_id=event.workspace_id,
                site_id=event.site_id,
            )
            status = "active"
        elif event.type == _SUB_RENEWED:
            # Already live — just refresh the annual renewal date (no re-deploy).
            await sites_service.mark_site_subscription(
                workspace_id=event.workspace_id,
                site_id=event.site_id,
                status="active",
                subscription_id=event.subscription_id or None,
                annual_renewal_date=datetime.now(UTC) + timedelta(days=365),
            )
            status = "active"
        elif event.type == _SUB_CANCELLED:
            # Mark cancelled; do NOT undeploy the live site in v1.
            # TODO(billing-lapse): a cancelled per-site sub is recorded but the live
            # site keeps serving. Full lapse / teardown / dunning — grace-period
            # length, undeploy-vs-grace, buyer notifications — is a DELIBERATE
            # follow-up pending a product decision on the grace policy. Until that
            # lands, cancellation is visibility-only: the status flips to
            # "cancelled" and the site stays up. Do NOT add an undeploy here without
            # that policy.
            await sites_service.mark_site_subscription(
                workspace_id=event.workspace_id,
                site_id=event.site_id,
                status="cancelled",
                subscription_id=event.subscription_id or None,
                annual_renewal_date=None,
            )
            status = "cancelled"
        else:
            # on_hold / paused / failed / expired / plan_changed / updated — acked,
            # no site-state action in v1.
            logger.info(
                "billing.webhook: ignoring per-site subscription event type=%s (site=%s)",
                event.type,
                event.site_id,
            )
            return {"ok": True, "granted": False}
    except NotFound:
        # A verified delivery for a site that doesn't exist for this workspace
        # (deleted, or a stale/cross-tenant id) — ack (200) so Dodo stops
        # retrying, but take no action. Nothing to update.
        logger.warning(
            "billing.webhook: per-site %s for unknown site=%s (event_id=%s) — ignoring",
            event.type,
            event.site_id,
            event.event_id,
        )
        return {"ok": True, "granted": False}

    logger.info(
        "billing.webhook: per-site %s for site=%s → %s (event_id=%s)",
        event.type,
        event.site_id,
        status,
        event.event_id,
    )
    return {"ok": True, "granted": False}


async def _upsert_subscription(event: SubscriptionEvent, *, status: str, plan_key: str) -> None:
    """Upsert the human-facing ``Subscription`` record. Idempotent.

    Keyed on the unique ``(gateway, gateway_subscription_id)`` index: a first
    activation inserts, a renewal / cancellation of a known subscription updates
    the existing row's status / plan.

    B2 review fix — a missing / falsy ``subscription_id`` is SKIPPED (logged), not
    recorded with an empty id. Two different workspaces whose verified events both
    carry no subscription_id would otherwise collide on the unique
    ``(gateway, "")`` key — the second delivery would overwrite the FIRST
    workspace's audit row (cross-tenant corruption). The grant + plan change still
    proceed in the caller (the BC-1 ledger is the money guard); only this
    audit-trail row is skipped. The index is intentionally left unchanged.
    """
    if not event.subscription_id:
        logger.warning(
            "billing.webhook: %s for workspace=%s carried no subscription_id "
            "(event_id=%s) — skipping Subscription audit upsert (grant/plan still applied)",
            event.type,
            event.workspace_id,
            event.event_id,
        )
        return

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
    "cancel",
    "create_topup",
    "handle_webhook",
    "subscribe",
]
