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
# the GRANT on ``currency == "USD"`` — a verified non-USD success event is acked
# but never granted (it would otherwise credit 1:1 against the wrong denomination).
# It is still RECORDED, with ``credits_granted=0``: the gate stops us handing over
# credits, not from keeping the receipt a later reversal has to join through.
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
#   SITE's ``subscription_status`` / ``renewal_date`` (active/renewed →
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
# Updated 2026-07-09 (fix/cancel-webhook-revert-guard): the ``subscription.cancelled``
#   plan revert is no longer UNCONDITIONAL. Webhook delivery is unordered /
#   at-least-once, so a ``cancelled(old_sub)`` retry can land AFTER ``active(new_sub)``
#   during a plan switch and would previously downgrade a paying customer to free
#   (nothing self-heals it — only ``subscription.active`` re-sets the plan). The
#   handler now marks THIS sub cancelled first, then reverts to free ONLY when no
#   OTHER active Subscription row still owns the workspace; a stale/out-of-order
#   cancel is logged and skipped. Credits are still never clawed back. See the
#   ``subscribe`` body for the STILL-OPEN downgrade lag-window (checkout→active
#   webhook) that only the atomic Dodo ``change_plan`` closes.
#
# Updated 2026-08-26 (feat/site-plans-as-addons): added ``_site_addon_cart`` +
#   ``sync_site_addons`` — a paid SITE is now a LINE on the workspace's existing
#   subscription instead of a subscription of its own. One bill, one renewal date,
#   one payment method, however many sites the workspace runs.
#
#   THE CART IS DECLARATIVE, WHICH IS THE ONLY THing worth knowing before touching
#   this. Dodo's ``change_plan`` REPLACES the add-on list; sending nothing removes
#   every add-on the subscription holds. So the cart is rebuilt from the ``Site``
#   documents on every call and pushed WHOLE — never appended to, never diffed
#   against gateway state. That also makes cancellation fall out for free: a site
#   that stops being active stops appearing in the cart, and the next sync drops
#   its line.
#
#   Sites still holding a per-site ``subscription_id`` are EXCLUDED from the cart.
#   Those are the subscriptions the old rail sold, Dodo is already billing them,
#   and counting one here too would charge the customer twice for one site.
#
#   A workspace with no active subscription is REFUSED (``NoActiveSubscription``)
#   rather than being sold a standalone per-site subscription — that standalone
#   subscription is exactly the separate payment this change removes.
#
# Updated 2026-09-02 (fix/billing-reversals-and-dunning): two money paths that
#   were acked and then dropped now do something.
#
#   (M1) REVERSALS. ``refund.succeeded`` and ``dispute.lost`` debit the credits
#   the payment granted (``_handle_reversal_event``). Nothing else moves money:
#   the other five ``dispute.*`` deliveries are lifecycle, and reversing when a
#   dispute OPENS would take the credits twice the moment it is later won. This
#   is not a refund policy — we do not issue refunds, but a chargeback is
#   involuntary and Dodo is merchant of record, so both paths arrive whether we
#   consent or not. The balance is allowed to go NEGATIVE, which blocks further
#   spend until it is settled; writing the shortfall off would make disputing
#   profitable. Idempotency keys on the webhook event id exactly as the grant
#   path does.
#
#   (M1a) The reversal join needs something to join TO, so ``_record_payment``
#   moved from the very end of ``handle_webhook`` to above the amount and
#   currency gates. Every verified success that carries a routable workspace now
#   leaves a ``Payment`` row whether or not it granted — previously the currency
#   gate returned first, so the population most likely to demand a refund (the
#   non-USD charges that take money and grant nothing) was precisely the one
#   with no record to trace. GRANT BEHAVIOUR IS UNCHANGED; only the record is.
#   Recording refused payments forced a field split the row wanted anyway:
#   ``amount_credits`` is what was PAID, ``credits_granted`` is what we actually
#   handed over, and the second is the cap a clawback runs against.
#
#   (M5) DUNNING. ``subscription.on_hold`` stamps a ``grace_until`` and leaves
#   the plan alone; ``sweep_subscription_grace`` revokes it once the deadline
#   passes; ``renewed`` / ``active`` clear the state and restore the plan;
#   ``expired`` / ``failed`` are terminal at the gateway so they suspend at once.
#   No dunning path ever claws back credits. The state rides ``subscription.*``
#   rather than Dodo's ``dunning.*`` events, which are a per-business toggle that
#   may be off and carry no metadata to route on.
#
#   THE TRAP THAT MAKES M5 MORE THAN A ONE-LINER: ``_active_subscription`` used
#   to filter ``status == "active"`` and is now ``_billable_subscription`` over
#   {active, on_hold}. Writing "on_hold" into that field WITHOUT widening the
#   predicate makes ``subscribe``'s guard stop seeing the row, skip the
#   cancel-then-create, and open a SECOND parallel Dodo subscription — the
#   double-billing defect fixed on 2026-07-08, reintroduced by a status string.
#   See ``_BILLABLE_STATUSES`` for which question each set answers.
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from beanie.operators import In
from dateutil.relativedelta import relativedelta
from pymongo import ReturnDocument
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
    ReversalEvent,
    SubscriptionEvent,
)
from pocketpaw_ee.cloud.billing.providers.base import IPaymentsProvider
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.subscription import Subscription

logger = logging.getLogger(__name__)

_GATEWAY = "dodo"
# Only this event type grants credits. The other one-time families
# (payment.failed / payment.processing / …) are acknowledged but never grant.
# Refunds and disputes do not merely fail to grant — they REVERSE; see
# ``_REVERSAL_EVENTS`` and ``_handle_reversal_event``.
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

# --- Dunning (M5) ---------------------------------------------------------
# A failing card arrives as ``subscription.on_hold``, NOT on one of Dodo's
# ``dunning.*`` events: dunning is a per-business toggle that may be off, it
# carries no metadata, and it would need a domain shape of its own — while
# ``subscription.*`` already arrives normalized with the workspace_id we need.
_SUB_ON_HOLD = "subscription.on_hold"
_SUB_EXPIRED = "subscription.expired"
_SUB_FAILED = "subscription.failed"
# Terminal at the GATEWAY: Dodo has stopped this subscription, so there is
# nothing left to wait for and the entitlements go at once, with no grace.
_SUB_TERMINAL_EVENTS = frozenset({_SUB_EXPIRED, _SUB_FAILED})

# ``Subscription.status`` vocabulary. ``active`` / ``cancelled`` predate dunning;
# ``on_hold`` and ``expired`` arrive with it.
_STATUS_ACTIVE = "active"
_STATUS_ON_HOLD = "on_hold"
_STATUS_EXPIRED = "expired"
_STATUS_CANCELLED = "cancelled"

# THE PREDICATE SPLIT, and it is the whole reason dunning is not a one-liner.
#
# BILLABLE — "the gateway subscription this workspace currently holds". An
# ``on_hold`` subscription is still a subscription: Dodo is still retrying the
# card and may still recover it, so opening a second one alongside it is a
# DOUBLE CHARGE. This is the set ``subscribe``'s guard, ``cancel`` and
# ``sync_site_addons`` ask for. Narrowing it back to {active} reintroduces the
# double-billing defect fixed on 2026-07-08 — the moment ``on_hold`` started
# being written into the status field, a ``status == "active"`` filter stopped
# seeing the row and the cancel-then-create guard was skipped.
_BILLABLE_STATUSES = frozenset({_STATUS_ACTIVE, _STATUS_ON_HOLD})
# PAID UP — "a subscription that is actually collecting". Deliberately narrower,
# and used only where the question is whether some OTHER subscription is still
# paying for this workspace: the grace sweep asks that before revoking a plan,
# and the row it is sweeping is itself ``on_hold``, so the billable set would
# match the very row in question.
#
# ENTITLEMENTS ARE RESOLVED FROM NEITHER SET. ``entitlements.resolve_entitlements``
# reads ``Workspace.plan``, so THAT field is the paid-up signal, and dunning
# revokes entitlements by moving it to ``free``. During grace the plan is
# deliberately left alone — that is what grace means.
_PAID_UP_STATUSES = frozenset({_STATUS_ACTIVE})

# --- Reversals (M1) -------------------------------------------------------
# The only two reversal events that move money. The other five ``dispute.*``
# deliveries are lifecycle: reversing on ``dispute.opened`` would take the
# credits twice the moment the dispute is later won, and ``refund.failed``
# reversed nothing at all.
_REFUND_SUCCEEDED = "refund.succeeded"
_DISPUTE_LOST = "dispute.lost"
_REVERSAL_EVENTS = frozenset({_REFUND_SUCCEEDED, _DISPUTE_LOST})
# The ledger cause a clawback is written under. Deliberately NOT one of the
# spend causes (``compute_spend`` / ``litellm_spend``): a reversal is money
# going back out, not usage, so it must never inflate the usage chart or eat the
# workspace's monthly quota.
_REVERSAL_CAUSE = "payment_reversal"


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


async def _subscription_with_status(
    workspace_id: str, statuses: frozenset[str]
) -> Subscription | None:
    """The workspace's most recent subscription in one of ``statuses``, or None.

    The ``Subscription`` ``(workspace)`` index is NON-UNIQUE — a workspace
    accumulates historical rows over its lifetime (a prior tier it switched off,
    an earlier subscription that was cancelled). So NEVER take a naive
    first-match: filter on status and, if more than one qualifies, take the
    most-recent (``-createdAt``) for a deterministic pick.

    WHICH SET a caller passes is a real decision, not a detail — see
    ``_BILLABLE_STATUSES`` and ``_PAID_UP_STATUSES``. Asking the narrow question
    where the wide one belongs is how a workspace ends up on two subscriptions.
    """
    return (
        await Subscription.find(
            Subscription.workspace == workspace_id,
            In(Subscription.status, sorted(statuses)),
        )
        .sort("-createdAt")
        .first_or_none()
    )


async def _billable_subscription(workspace_id: str) -> Subscription | None:
    """The gateway subscription this workspace currently HOLDS, or None.

    ``active`` or ``on_hold``. A subscription whose card is failing is still a
    subscription — Dodo is retrying it and may still recover it — so it must be
    cancelled before a replacement opens, it must remain cancellable by its
    owner, and site add-ons still attach to it. Everything that asks "does this
    workspace already have a subscription?" asks this.
    """
    return await _subscription_with_status(workspace_id, _BILLABLE_STATUSES)


async def _paid_up_subscription(workspace_id: str) -> Subscription | None:
    """A subscription that is actually COLLECTING for this workspace, or None.

    Strictly ``active``. Used where the question is whether some OTHER
    subscription is still paying — the grace sweep asks it before revoking a
    plan, and it cannot use the billable set there because the row it is
    sweeping is itself ``on_hold`` and would match.
    """
    return await _subscription_with_status(workspace_id, _PAID_UP_STATUSES)


async def _subscription_by_gateway_id(subscription_id: str) -> Subscription | None:
    """The row tracking one gateway subscription, or None. Keyed on the unique
    ``(gateway, gateway_subscription_id)`` index."""
    if not subscription_id:
        return None
    return await Subscription.find_one(
        Subscription.gateway == _GATEWAY,
        Subscription.gateway_subscription_id == subscription_id,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Read a stored datetime back as tz-aware UTC.

    BSON carries no timezone, so a datetime that round-trips through Mongo can
    come back NAIVE while ``datetime.now(UTC)`` is aware, and comparing the two
    raises TypeError. Every deadline read off a document goes through here.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _dunning_grace_days() -> int:
    """Days a workspace keeps its plan after a payment fails, from settings."""
    from pocketpaw.config import get_settings

    return max(int(get_settings().billing_dunning_grace_days), 0)


async def _site_addon_cart(workspace_id: str) -> list[dict]:
    """The COMPLETE Dodo add-on cart a workspace's sites should be billed for.

    Rebuilt from the ``Site`` documents every time, never from what the gateway
    currently holds. That is not defensiveness, it is the contract:
    ``change_plan`` REPLACES the whole add-on list, so the only safe thing to
    send is a full cart derived from our own source of truth. A function that
    read the gateway's cart and appended to it would inherit any drift already
    there and make it permanent.

    Three exclusions, each load-bearing:

      * A site holding a ``subscription_id`` is on a LEGACY per-site
        subscription. Dodo is already billing it on its own rail, and counting it
        here too would charge the customer twice for one site. Only sites with no
        per-site subscription of their own ride the add-on rail.
      * A site whose ``subscription_status`` is not active is not paying — a
        cancelled site must drop off the next cart, which is precisely how a
        cancellation stops costing money under this model.
      * A tier with no configured ``dodo_addon_id`` cannot be expressed as an
        add-on. It is skipped rather than guessed at; the publish path already
        refuses to record an unpurchasable tier.

    Quantities aggregate: four sites on ``site`` are one cart line of quantity 4,
    not four lines. Dodo keys a cart line by add-on id, so emitting the same id
    twice would be a malformed cart rather than a double charge.

    Sorted by add-on id so the cart is deterministic — two calls with the same
    sites produce byte-identical payloads, which is what makes a no-op sync
    genuinely a no-op and keeps test assertions stable.
    """
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.models.site import Site

    counts: dict[str, int] = {}
    async for doc in Site.find(Site.workspace == workspace_id):
        if getattr(doc, "subscription_id", None):
            continue
        if (getattr(doc, "subscription_status", None) or "none") != "active":
            continue
        tier = site_plans.site_scoped_tier(getattr(doc, "plan_tier", None))
        if tier is None or tier.monthly_price_usd == 0:
            continue
        addon_id = tier.dodo_addon_id
        if not addon_id:
            continue
        counts[addon_id] = counts.get(addon_id, 0) + 1
    return [{"addon_id": addon_id, "quantity": qty} for addon_id, qty in sorted(counts.items())]


async def sync_site_addons(
    workspace_id: str,
    *,
    provider: IPaymentsProvider | None = None,
) -> dict:
    """Push the workspace's full site add-on cart onto its EXISTING subscription.

    This is how a paid site is billed now: as a line on the one subscription the
    workspace already has, rather than as a subscription of its own. One bill,
    one renewal date, one payment method, and a per-site charge that prorates
    against the term the workspace has already paid for.

    Idempotent by construction. The cart is recomputed from the ``Site``
    documents on every call and sent whole, so calling this twice with no change
    between sends the same cart twice and the second is a no-op at the gateway.
    Callers do not need to know whether a sync is "needed".

    RAISES ``NoActiveSubscription`` WHEN THE WORKSPACE HAS NO SUBSCRIPTION, and
    that refusal is the deliberate shape of the feature rather than a gap in it.
    An add-on attaches to something; there is no subscription-less add-on at this
    gateway. A free workspace buying its first site therefore has to start a
    workspace subscription, and the alternative — quietly opening a standalone
    per-site subscription for it — is the separate payment this change exists to
    remove. Reversing that trade is one branch here (create a subscription with
    the cart attached, rather than refusing), but it needs a target workspace
    plan that the publish request does not carry today, so it is not guessed.
    """
    sub = await _billable_subscription(workspace_id)
    if sub is None:
        raise NoActiveSubscription(
            "This workspace has no active subscription to add a site plan to."
        )
    # The gateway needs the plan the subscription is ALREADY on: ``change_plan``
    # is one call that sets both the product and the cart, so "keep the plan,
    # change the cart" is expressed by re-sending the current product. Prefer the
    # product recorded on the row over a catalog lookup — the row is what the
    # gateway actually charged, and a catalog remapped since the sale would
    # otherwise silently move the workspace's plan as a side effect of publishing
    # a site.
    product_id = sub.product_id or _dodo_product_for_plan(sub.plan_key)
    if not product_id:
        raise ValidationError(
            "billing.plan_product_unconfigured",
            f"No Dodo product is configured for plan '{sub.plan_key}'.",
        )
    if not sub.gateway_subscription_id:
        raise ValidationError(
            "billing.invalid_subscription",
            "The active subscription has no gateway id to attach add-ons to.",
        )

    cart = await _site_addon_cart(workspace_id)
    prov = provider or _default_provider()
    await prov.change_plan(
        subscription_id=sub.gateway_subscription_id,
        product_id=product_id,
        plan_key=sub.plan_key,
        addons=cart,
    )
    logger.info(
        "billing.sync_site_addons: workspace=%s subscription=%s cart=%s",
        workspace_id,
        sub.gateway_subscription_id,
        cart,
    )
    return {
        "subscription_id": sub.gateway_subscription_id,
        "plan_key": sub.plan_key,
        "addons": cart,
    }


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
    # KNOWN LIMITATION — DOWNGRADE LAG WINDOW (not fixed here; needs change_plan).
    # ``_billable_subscription`` reads the LOCAL Subscription row, and only the
    # ``subscription.active`` webhook writes ``status="active"``. Between checkout
    # COMPLETION (buyer paid) and that ``active`` webhook LANDING, no local active
    # row exists yet, so a fast SECOND switch in that window sees ``existing is None``,
    # skips the cancel-then-create guard, and opens a SECOND parallel gateway
    # subscription = double-charge — the exact defect this branch set out to fix.
    #
    # Why we do NOT close it by querying the gateway here: Dodo's
    # ``subscriptions.list`` filters only by ``customer_id`` / ``product_id`` /
    # ``status`` / dates — there is NO metadata filter, and we store no per-workspace
    # Dodo ``customer_id`` (each ``create_subscription`` mints a fresh customer from
    # the email). So "list THIS workspace's active subs at the gateway" would mean
    # paging EVERY active subscription in the business and filtering client-side on
    # ``metadata.workspace_id`` — fragile and unbounded. We deliberately do not add
    # that under pressure.
    #
    # REAL FIX (follow-up): Dodo's SDK exposes an ATOMIC
    # ``subscriptions.change_plan`` (proration, no re-checkout, no drop-to-free
    # window) that removes cancel-then-create ENTIRELY — no lag window, no second
    # subscription, no "buyer abandons the new checkout after the old is cancelled"
    # gap. Wiring it is a scoped change: a new provider-port method, a
    # ``subscription.plan_changed`` webhook handler (grant + plan + audit; the
    # service acts on active/renewed/cancelled only today), and a /subscribe
    # response-contract change (change_plan returns no checkout url). Tracked as the
    # billing plan-change follow-up; until it lands the lag window above remains.
    existing = await _billable_subscription(workspace_id)

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
    """Cancel the workspace's BILLABLE recurring subscription at the gateway.

    Loads the workspace's billable ``Subscription`` row and tells the gateway to
    stop billing it (``provider.cancel_subscription``). Returns ``{"ok": True}``.

    BILLABLE, not strictly active: a subscription in dunning (``on_hold``) is
    still being charged for and must remain cancellable, or a buyer whose card is
    failing gets a 402 when they try to stop the retries.

    The entitlement revert (``Workspace.plan`` -> free) and the Subscription-row
    status flip are NOT done here — they land REACTIVELY on the verified
    ``subscription.cancelled`` webhook (mirroring how ``subscribe`` defers the
    upgrade to the ``subscription.active`` webhook; the webhook handler is the sole
    writer of that plan mutation, so cancelling here would duplicate it).

    Raises 402 ``billing.no_active_subscription`` when the workspace has no
    billable subscription — only historical / already-cancelled / expired rows,
    or never subscribed. The error code keeps its wire name; only the predicate
    behind it widened.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("billing.invalid_workspace", "workspace_id is required")

    active = await _billable_subscription(workspace_id)
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
    parses to a ``SubscriptionEvent`` (BC-7 renewal grant + plan change, and M5
    dunning); a ``refund.*`` / ``dispute.*`` delivery parses to a
    ``ReversalEvent`` (M1 clawback), whose ack carries a ``reversed`` count
    beside ``granted``.

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
    # SubscriptionEvent (BC-7); money going back out is a ReversalEvent (M1);
    # everything else is the BC-2 one-time GatewayEvent.
    if isinstance(event, SubscriptionEvent):
        return await _handle_subscription_event(event)
    if isinstance(event, ReversalEvent):
        return await _handle_reversal_event(event)

    # Only a success event grants. Everything else (failed / processing) is
    # acknowledged so the gateway stops retrying, but never grants.
    if event.type != _SUCCESS_EVENT:
        logger.info("billing.webhook: ignoring non-success event type=%s", event.type)
        return {"ok": True, "granted": False}

    if not event.workspace_id:
        # A success event with no workspace_id in metadata can't be routed. Ack
        # it (200) so the gateway stops retrying, but record nothing.
        #
        # This is the ONE verified success that still writes no row, and the
        # reason is that there is no tenant to scope it to: ``Payment.workspace``
        # is the tenant boundary, and a row carrying an empty one is unroutable
        # for the reversal join it would exist to serve.
        logger.warning(
            "billing.webhook: payment.succeeded carried no workspace_id (event_id=%s) — ignoring",
            event.event_id,
        )
        return {"ok": True, "granted": False}

    # RECORD FIRST, GRANT SECOND (M1a). This call used to sit at the very end of
    # the function, below every guard, so a payment that was acked WITHOUT
    # granting left no ``Payment`` row at all — and the population that produced
    # was exactly the one most likely to demand a refund, the non-USD charges
    # that take the money and grant nothing. A reversal joins through
    # ``payment_id`` -> ``gateway_ref``, so no row meant no clawback was even
    # possible.
    #
    # Moving it above the gates also closes a smaller race it was creating on
    # the happy path: running after the grant left a window where credits
    # existed with no payment record behind them.
    #
    # The row goes in with ``credits_granted=0`` and is stamped with the real
    # figure once the grant lands. That ordering is deliberate — a crash between
    # the two leaves us UNDER-recording what we gave, and a reversal that claws
    # back too little is recoverable in a way that one clawing back credits the
    # buyer never received is not.
    await _record_payment(event)

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

    # Stamp what the grant ACTUALLY moved onto the row written above. Done on
    # every delivery, not only a first one: if a prior delivery crashed in the
    # window between recording and granting, its row still reads 0 while the
    # credits exist, and a redelivery is the only thing that can heal it.
    await _mark_payment_granted(event.event_id, event.amount_credits)

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
    """Record a VERIFIED ``payment.succeeded``, granted or not. Idempotent.

    Every verified success that carries a routable workspace gets a row —
    including the ones acked without granting. That is not bookkeeping
    completeness for its own sake: a reversal has no metadata to route on and
    joins through ``payment_id`` -> ``gateway_ref``, so a payment with no row is
    a payment whose refund can never be traced to a wallet.

    ``amount_credits`` is what the buyer PAID. ``credits_granted`` starts at 0
    and is stamped by ``_mark_payment_granted`` once a grant lands, so a refused
    payment keeps 0 and a reversal against it correctly takes nothing.

    ``status`` stays the GATEWAY's outcome and is unchanged by any of this: a
    non-USD charge is genuinely ``succeeded``, it just granted nothing.

    A replayed webhook collides on the unique ``(gateway, gateway_event_id)``
    index → ``DuplicateKeyError`` → we treat it as already recorded (no-op).
    """
    doc = Payment(
        workspace=event.workspace_id,
        gateway=_GATEWAY,
        gateway_ref=str((event.raw.get("data") or {}).get("payment_id") or "") or None,
        gateway_event_id=event.event_id,
        amount_credits=event.amount_credits,
        credits_granted=0,
        currency=event.currency or None,
        status="succeeded",
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        # Already recorded by a prior delivery of the same event — no-op.
        return


async def _mark_payment_granted(event_id: str, credits_granted: int) -> None:
    """Stamp what a payment actually granted onto its recorded row.

    Separate from ``_record_payment`` because the row is written BEFORE the
    grant (so it exists even when the grant never happens) while this figure is
    only known after. Idempotent by construction — it sets a value rather than
    incrementing one, so a redelivery writes the same number and a delivery that
    crashed before reaching here is healed by the next one.
    """
    await Payment.get_pymongo_collection().update_one(
        {"gateway": _GATEWAY, "gateway_event_id": event_id},
        {"$set": {"credits_granted": int(credits_granted)}, "$currentDate": {"updatedAt": True}},
    )


def _reversal_ack(reversed_credits: int = 0) -> dict:
    """The ack shape for a reversal delivery.

    ``granted`` is always False (a reversal never adds credits) and ``reversed``
    is what actually left the wallet on THIS delivery — 0 for a lifecycle event,
    an unjoinable payment, or a redelivery.
    """
    return {"ok": True, "granted": False, "reversed": int(reversed_credits)}


async def _handle_reversal_event(event: ReversalEvent) -> dict:
    """Claw credits back for a VERIFIED reversal (signature already checked).

    Acts on ``refund.succeeded`` and ``dispute.lost``, and on nothing else. The
    other five ``dispute.*`` deliveries are lifecycle — a dispute that opens may
    still be won, and reversing when it opens would take the credits twice — and
    ``refund.failed`` moved no money at all. Those are acked and logged.

    WHY THIS EXISTS UNDER A NO-REFUND POLICY. The decision not to issue refunds
    governs what WE initiate; it does not govern what arrives. A chargeback is
    involuntary — ``dispute.lost`` means the issuer has already taken the cash
    back — and Dodo is merchant of record unless a BYOP route is configured, so
    Dodo can refund unilaterally to protect its own dispute ratio. The handler is
    the same code under either policy.

    THE JOIN. A ``Dispute`` carries no metadata, so ``payment_id`` is the only
    key back to a wallet: it matches ``Payment.gateway_ref``, which is written
    for every verified success (see ``_record_payment`` — recording the refused
    ones too is exactly what makes a refunded non-USD charge joinable).

    THE CAP is ``credits_granted`` on that row, less whatever earlier reversals
    already took. It is doing three jobs at once, which is why this path needs no
    currency gate and no partial-refund special case of its own:

      * a non-USD charge granted nothing, so its cap is 0 and refunding it takes
        no credits — correct, because we never gave any;
      * a payment carrying BOTH a refund and a lost dispute (routine, because
        Verifi RDR resolves disputes by refunding) cannot be clawed twice;
      * we can never take more credits than we handed out, whatever the gateway
        says the money was.

    THE AMOUNT is the reversal's own stated figure when the gateway named one in
    the payment's currency — a partial refund returns part of the charge, and
    taking the whole grant for it would seize credits the buyer still paid for.
    Otherwise it is the full remaining grant, which is what a completed refund
    and a lost chargeback both mean.

    UNLESS the gateway said BOTH "this is partial" and nothing we could parse as
    an amount. Those two claims contradict each other, and defaulting to the full
    grant there would over-reverse on the strength of a number we could not read.
    That case takes nothing and logs at ERROR: an under-reversal is recoverable
    by hand, money taken from a customer who did not owe it is not.

    IDEMPOTENCY is the ledger's, keyed on the webhook event id exactly as the
    grant path keys on it, so a redelivery collides on BC-1's unique
    ``(workspace, idempotency_key)`` index and moves nothing. The debit runs
    FIRST and the row's running total is claimed second under a ``$ne`` filter on
    the event id: money is never left un-moved because a bookkeeping write
    failed, and a crash between the two heals on the next delivery.

    THE BALANCE IS ALLOWED TO GO NEGATIVE. ``debit(allow_negative=True)`` never
    raises, and a negative balance blocks further spend (``check_balance``
    rejects at <= 0) until it is settled. Writing the shortfall off instead would
    make disputing profitable.

    KNOWN GAP — a subscription RENEWAL cannot be reversed here, and the reason is
    structural rather than unfinished. A renewal writes no ``Payment`` row
    because ``subscription.active`` / ``.renewed`` carry no ``payment_id``
    anywhere in their body, so nothing links a refunded renewal charge back to
    the grant it paid for; and that grant is the tier's monthly ALLOTMENT, not
    the cash, so the money on the refund is not the figure to claw back either.
    Such a delivery is logged at ERROR with the payment id and left for a human.
    Reversing a number nobody can derive would be worse than alarming.
    """
    if event.type not in _REVERSAL_EVENTS:
        logger.info(
            "billing.webhook: %s is reversal lifecycle (payment=%s, event_id=%s) — "
            "acked, no money action",
            event.type,
            event.payment_id,
            event.event_id,
        )
        return _reversal_ack()

    if not event.payment_id:
        logger.error(
            "billing.webhook: %s carried no payment_id (event_id=%s) — nothing to join a "
            "reversal to; acked without clawing back",
            event.type,
            event.event_id,
        )
        return _reversal_ack()

    doc = await Payment.find_one(
        Payment.gateway == _GATEWAY,
        Payment.gateway_ref == event.payment_id,
    )
    if doc is None:
        logger.error(
            "billing.webhook: %s for payment=%s (event_id=%s) matches no recorded payment — "
            "NOT clawing back. A subscription renewal writes no Payment row (its webhook "
            "carries no payment_id), so a renewal reversal lands here and needs a human",
            event.type,
            event.payment_id,
            event.event_id,
        )
        return _reversal_ack()

    # A row written before ``credits_granted`` existed carries None. Those rows
    # were ONLY ever written when the grant applied, so "missing" means "granted
    # everything that was paid" — reading it as 0 would quietly make every
    # pre-existing payment un-reversible.
    granted = doc.amount_credits if doc.credits_granted is None else doc.credits_granted
    already = int(doc.credits_reversed or 0)
    remaining = max(int(granted) - already, 0)
    if remaining <= 0:
        logger.info(
            "billing.webhook: %s for payment=%s (event_id=%s) — nothing left to reverse "
            "(granted=%d, already reversed=%d)",
            event.type,
            event.payment_id,
            event.event_id,
            int(granted),
            already,
        )
        return _reversal_ack()

    same_currency = bool(event.currency) and event.currency.upper() == (doc.currency or "").upper()
    if event.amount_credits > 0 and same_currency:
        amount = min(event.amount_credits, remaining)
    elif event.is_partial:
        # A PARTIAL reversal whose amount we could not read. Falling through to
        # the full remaining grant here would seize credits the buyer still paid
        # for, on the strength of a number we admit we could not parse. The
        # gateway has told us two things and they disagree, so we take nothing
        # and alarm — an under-reversal is recoverable by hand, an over-reversal
        # is money taken from a customer who did not owe it.
        logger.error(
            "billing.webhook: %s for payment=%s (event_id=%s) is PARTIAL but named no usable "
            "amount in %s — NOT clawing back; reversing the full grant would take credits the "
            "buyer still paid for. Needs a human",
            event.type,
            event.payment_id,
            event.event_id,
            doc.currency or "the payment's currency",
        )
        return _reversal_ack()
    else:
        amount = remaining

    balance = await credits_service.debit(
        workspace=doc.workspace,
        amount=amount,
        cause=_REVERSAL_CAUSE,
        idempotency_key=event.event_id,
        allow_negative=True,
        ref={
            "gateway": _GATEWAY,
            "event_id": event.event_id,
            "payment_id": event.payment_id,
            "reason": event.type,
        },
    )

    # Claim the reversal on the payment row. The ``$ne`` filter is the
    # exactly-once guard for the running total: a redelivery that somehow got
    # past the remaining-credits check above finds its event id already listed
    # and adds nothing.
    claimed = await Payment.get_pymongo_collection().find_one_and_update(
        {"_id": doc.id, "reversal_event_ids": {"$ne": event.event_id}},
        {
            "$inc": {"credits_reversed": amount},
            "$push": {"reversal_event_ids": event.event_id},
            "$currentDate": {"updatedAt": True},
        },
        return_document=ReturnDocument.AFTER,
    )
    if claimed is None:
        logger.info(
            "billing.webhook: %s for payment=%s (event_id=%s) was already applied — the debit "
            "was a no-op, balance unchanged",
            event.type,
            event.payment_id,
            event.event_id,
        )
        return _reversal_ack()

    logger.warning(
        "billing.webhook: %s reversed %d credits from workspace=%s (payment=%s, event_id=%s) — "
        "balance now %d",
        event.type,
        amount,
        doc.workspace,
        event.payment_id,
        event.event_id,
        balance,
    )
    return _reversal_ack(amount)


# ---------------------------------------------------------------------------
# Subscription webhook handling (BC-7)
# ---------------------------------------------------------------------------


async def _handle_subscription_event(event: SubscriptionEvent) -> dict:
    """Act on a VERIFIED ``subscription.*`` webhook (signature already checked).

    * ``subscription.active`` / ``subscription.renewed`` → grant the tier's
      monthly allotment ADDITIVELY (unused credits roll over) keyed on the
      per-renewal ``event_id``; ``active`` ALSO upgrades ``Workspace.plan`` and
      resyncs the stored seat cap UP to the new plan (upgrade-only — a later
      cancel reverts the plan but never strips seats).
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
        # Mark THIS subscription cancelled FIRST, then decide whether to revert the
        # plan. Webhook delivery is unordered / at-least-once: during a plan switch a
        # ``cancelled(old_sub)`` retry can land AFTER ``active(new_sub)``. Reverting
        # to free UNCONDITIONALLY there would strand a paying customer on free
        # entitlements — and nothing self-heals it (only subscription.active re-sets
        # the plan; renewed does not). So flip this sub's row to cancelled, then ask
        # whether any OTHER active subscription still owns the workspace:
        #   * a newer active sub exists -> SKIP the revert (a stale cancel must not
        #     downgrade the live plan);
        #   * none -> revert to free (the normal cancel, and the common
        #     cancel-then-active ordering still self-corrects because active arrives
        #     later and re-sets the plan).
        # Credits are NEVER clawed back either way.
        await _upsert_subscription(event, status="cancelled", plan_key="free")
        still_active = await _billable_subscription(event.workspace_id)
        if still_active is not None:
            logger.info(
                "billing.webhook: subscription.cancelled for workspace=%s (event_id=%s) — "
                "a newer active subscription=%s still owns the workspace; NOT reverting to "
                "free (out-of-order/stale cancel)",
                event.workspace_id,
                event.event_id,
                still_active.gateway_subscription_id,
            )
            return {"ok": True, "granted": False}
        ok = await workspace_service.set_workspace_plan(event.workspace_id, "free")
        logger.info(
            "billing.webhook: subscription.cancelled for workspace=%s (event_id=%s) — "
            "no other active subscription; plan reverted to free=%s, credits NOT clawed back",
            event.workspace_id,
            event.event_id,
            ok,
        )
        return {"ok": True, "granted": False}

    # DUNNING (M5). A failing card no longer costs us nothing: on_hold starts a
    # grace clock, expired/failed suspend at once. Neither ever touches credits.
    if event.type == _SUB_ON_HOLD:
        return await _handle_dunning_hold(event)
    if event.type in _SUB_TERMINAL_EVENTS:
        return await _handle_dunning_terminal(event)

    if event.type not in _SUB_GRANT_EVENTS:
        # plan_changed / updated — acked, no money/plan action here. plan_changed
        # is deliberately still inert: a switch driven through ``change_plan``
        # persists the new tier at the call site, and reacting to the webhook too
        # would apply it twice.
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

    # Read the row BEFORE the upsert below clears the dunning stamps: a
    # grant-bearing delivery landing on a SUSPENDED subscription is the card
    # finally clearing, and the recovery leg needs to know that happened.
    prior = await _subscription_by_gateway_id(event.subscription_id)
    recovered = prior is not None and prior.suspended_at is not None

    # subscription.active upgrades the entitlement to the subscribed tier.
    if event.type == _SUB_ACTIVE:
        await workspace_service.set_workspace_plan(event.workspace_id, tier.key)
        # feat/billing-smb-caps: lift the stored seat cap to the new plan so an
        # upgrade actually raises it. UPGRADE-ONLY (max(doc.seats, plan.max_seats))
        # — a later cancel reverts the plan but never strips the seats. Best-effort:
        # a resync hiccup must not fail the (already-applied) credit grant / plan
        # move, and the seat gates also lift the ceiling live via the resolved plan.
        try:
            new_seats = await workspace_service.raise_seats_for_plan(event.workspace_id, tier.key)
            if new_seats is not None:
                logger.info(
                    "billing.webhook: %s resynced seat cap to %d for workspace=%s plan=%s",
                    event.type,
                    new_seats,
                    event.workspace_id,
                    tier.key,
                )
        except Exception:
            logger.exception(
                "billing.webhook: seat-cap resync failed for workspace=%s plan=%s "
                "(event_id=%s) — plan upgrade stands; seat gate still lifts live",
                event.workspace_id,
                tier.key,
                event.event_id,
            )
    elif recovered:
        # RECOVERY (M5). A renewal that lands on a suspended subscription means
        # the card finally cleared. ``active`` already re-sets the plan above;
        # ``renewed`` never did, so without this the buyer pays and stays on
        # free. Only fires when the row was actually suspended, so an ordinary
        # renewal writes nothing extra.
        ok = await workspace_service.set_workspace_plan(event.workspace_id, tier.key)
        logger.info(
            "billing.webhook: %s recovered suspended subscription=%s for workspace=%s "
            "(event_id=%s) — plan restored to %s=%s",
            event.type,
            event.subscription_id,
            event.workspace_id,
            event.event_id,
            tier.key,
            ok,
        )

    # Track the subscription lifecycle (active for both active + renewed), and
    # clear any dunning state — a grant-bearing delivery IS the payment working.
    await _upsert_subscription(
        event,
        status=_STATUS_ACTIVE,
        plan_key=tier.key,
        grace_until=None,
        suspended_at=None,
    )

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


async def _handle_dunning_hold(event: SubscriptionEvent) -> dict:
    """``subscription.on_hold`` — a payment failed. Start the grace clock.

    The entitlements are deliberately NOT touched here. On hold means Dodo is
    still retrying the card, and taking the plan away on the first declined
    charge would punish a customer whose bank said no once. What lands instead is
    a deadline: ``sweep_subscription_grace`` revokes the plan if it passes, and a
    successful retry (``renewed`` / ``active``) clears it.

    Credits are NEVER clawed back on this path. A failed renewal means next month
    was not paid for, not that last month was refunded — reversal belongs to
    ``refund.succeeded`` / ``dispute.lost`` and to nothing else.

    A REDELIVERY MUST NOT EXTEND THE WINDOW. Delivery is at-least-once, so
    re-stamping on every retry would hand a non-paying workspace a rolling grace
    period that never expires. The stamp lands only on the TRANSITION into
    on_hold; a genuine recover-then-fail-again cycle passes through ``active``
    first, which clears the deadline, so it re-stamps then.
    """
    existing = await _subscription_by_gateway_id(event.subscription_id)
    if existing is not None and existing.status == _STATUS_ON_HOLD and existing.grace_until:
        logger.info(
            "billing.webhook: subscription.on_hold for workspace=%s (event_id=%s) — already on "
            "hold until %s; not extending the grace window",
            event.workspace_id,
            event.event_id,
            existing.grace_until,
        )
        return {"ok": True, "granted": False}

    grace_until = datetime.now(UTC) + timedelta(days=_dunning_grace_days())
    await _upsert_subscription(event, status=_STATUS_ON_HOLD, grace_until=grace_until)
    logger.warning(
        "billing.webhook: subscription.on_hold for workspace=%s subscription=%s (event_id=%s) — "
        "payment failed; plan kept until %s, then suspended by the grace sweep",
        event.workspace_id,
        event.subscription_id,
        event.event_id,
        grace_until.isoformat(),
    )
    return {"ok": True, "granted": False}


async def _handle_dunning_terminal(event: SubscriptionEvent) -> dict:
    """``subscription.expired`` / ``.failed`` — dead at the gateway, no grace.

    Grace exists because a retry might still succeed. These two say it will not:
    Dodo has stopped the subscription. So the entitlements go now, on the same
    terms the cancellation path uses — and with the same out-of-order guard,
    because an ``expired`` for last month's subscription must not strip the plan
    a newly-active one just granted. Credits are not clawed back.
    """
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    await _upsert_subscription(
        event,
        status=_STATUS_EXPIRED,
        grace_until=None,
        suspended_at=datetime.now(UTC),
    )
    still_billable = await _billable_subscription(event.workspace_id)
    if still_billable is not None:
        logger.info(
            "billing.webhook: %s for workspace=%s (event_id=%s) — subscription=%s still owns the "
            "workspace; NOT reverting to free",
            event.type,
            event.workspace_id,
            event.event_id,
            still_billable.gateway_subscription_id,
        )
        return {"ok": True, "granted": False}

    ok = await workspace_service.set_workspace_plan(event.workspace_id, "free")
    logger.warning(
        "billing.webhook: %s for workspace=%s subscription=%s (event_id=%s) — no other billable "
        "subscription; plan reverted to free=%s, credits NOT clawed back",
        event.type,
        event.workspace_id,
        event.subscription_id,
        event.event_id,
        ok,
    )
    return {"ok": True, "granted": False}


async def sweep_subscription_grace() -> int:
    """Suspend every subscription still on hold past its grace deadline.

    The half of dunning a webhook cannot do. ``subscription.on_hold`` only stamps
    a deadline, and nothing arrives from the gateway when it passes, so a
    periodic pass is what actually turns "the payment failed a week ago" into
    "the entitlements are gone". Driven from the five-minute sweeper heartbeat in
    ``extensions.start_run_sweeper``; a deadline measured in days does not need a
    tighter tick. Returns how many subscriptions it suspended.

    THE ROW KEEPS ``status == "on_hold"`` and gains a ``suspended_at`` stamp
    rather than moving to a terminal status, and that split is load-bearing.
    Suspension is OURS: it revokes the entitlements we grant. The subscription
    itself is still alive at Dodo, still being retried, and still able to recover
    — so it stays BILLABLE, which is what keeps ``subscribe`` cancelling it
    before opening a replacement instead of leaving the buyer paying for two.
    ``suspended_at`` is also what makes the pass idempotent: an already-suspended
    row is skipped rather than re-revoked every five minutes.

    Never touches credits. A workspace that stops paying loses its plan, not the
    credits it already bought.
    """
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    now = datetime.now(UTC)
    suspended = 0
    async for sub in Subscription.find(Subscription.status == _STATUS_ON_HOLD):
        if sub.suspended_at is not None:
            continue
        deadline = _as_utc(sub.grace_until)
        if deadline is None or deadline > now:
            continue

        sub.suspended_at = now
        await sub.save()
        suspended += 1

        # Another subscription may have taken over mid-dunning (a plan switch
        # leaves the old row on hold). Ask for a PAID-UP one specifically — the
        # billable set would match the very row being swept.
        paying = await _paid_up_subscription(sub.workspace)
        if paying is not None:
            logger.info(
                "billing.sweep_subscription_grace: workspace=%s subscription=%s passed its grace "
                "deadline, but subscription=%s is still paying — plan kept",
                sub.workspace,
                sub.gateway_subscription_id,
                paying.gateway_subscription_id,
            )
            continue

        ok = await workspace_service.set_workspace_plan(sub.workspace, "free")
        logger.warning(
            "billing.sweep_subscription_grace: workspace=%s subscription=%s stayed on hold past "
            "%s — plan reverted to free=%s, credits NOT clawed back",
            sub.workspace,
            sub.gateway_subscription_id,
            deadline.isoformat(),
            ok,
        )
    if suspended:
        logger.info("billing.sweep_subscription_grace: suspended %d subscription(s)", suspended)
    return suspended


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
                # The authoritative gateway subscription id. Dodo creates the
                # subscription at payment time, so this is the FIRST delivery that
                # carries it — the site is still holding the ``cks_`` checkout
                # session id until we hand it over. Without it nothing downstream
                # can cancel or change the plan.
                subscription_id=event.subscription_id or None,
            )
            status = "active"
        elif event.type == _SUB_RENEWED:
            # Already live — just refresh the renewal date (no re-deploy).
            #
            # ONE MONTH, not 365 days. Site plans went monthly on 2026-08-22; a
            # renewal that stamps a year ahead on a monthly plan puts every
            # subsequent renewal a year out, and the site keeps its paid
            # capabilities for that whole year regardless of what happens to the
            # card. relativedelta rather than timedelta(days=30) so the date does
            # not drift backwards through the calendar over a year of renewals.
            await sites_service.mark_site_subscription(
                workspace_id=event.workspace_id,
                site_id=event.site_id,
                status="active",
                subscription_id=event.subscription_id or None,
                renewal_date=datetime.now(UTC) + relativedelta(months=1),
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
                renewal_date=None,
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


async def _upsert_subscription(
    event: SubscriptionEvent,
    *,
    status: str,
    plan_key: str | None = None,
    grace_until: datetime | None = None,
    suspended_at: datetime | None = None,
) -> None:
    """Upsert the human-facing ``Subscription`` record. Idempotent.

    Keyed on the unique ``(gateway, gateway_subscription_id)`` index: a first
    activation inserts, a renewal / cancellation of a known subscription updates
    the existing row's status / plan.

    ``plan_key=None`` means LEAVE THE TIER ALONE on an existing row (falling back
    to the event's own metadata on a fresh insert). The dunning paths use it:
    a subscription that went on hold is still on the tier it was sold, and
    overwriting that would destroy the record of what the buyer was paying for.

    ``grace_until`` / ``suspended_at`` are written on EVERY path, including the
    insert and the duplicate-key re-fetch. Writing them on only one path is how a
    racing insert keeps a stale deadline: the caller has decided what the dunning
    state is, so passing None must clear the field rather than skip it.

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

    existing = await _subscription_by_gateway_id(event.subscription_id)
    if existing is not None:
        existing.status = status
        if plan_key is not None:
            existing.plan_key = plan_key
        existing.grace_until = grace_until
        existing.suspended_at = suspended_at
        if event.product_id:
            existing.product_id = event.product_id
        await existing.save()
        return
    doc = Subscription(
        workspace=event.workspace_id,
        gateway=_GATEWAY,
        gateway_subscription_id=event.subscription_id,
        plan_key=plan_key if plan_key is not None else (event.plan_key or "free"),
        product_id=event.product_id or None,
        status=status,
        grace_until=grace_until,
        suspended_at=suspended_at,
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        # A racing delivery inserted it first — re-fetch and update instead.
        existing = await _subscription_by_gateway_id(event.subscription_id)
        if existing is not None:
            existing.status = status
            if plan_key is not None:
                existing.plan_key = plan_key
            existing.grace_until = grace_until
            existing.suspended_at = suspended_at
            await existing.save()


__all__ = [
    "cancel",
    "create_topup",
    "handle_webhook",
    "subscribe",
    "sweep_subscription_grace",
    "sync_site_addons",
]
