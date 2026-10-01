# ee/paw_bar/actions.py — the shared Paw Bar action executor.
#
# The SINGLE code path both the public POST /paw-bar/action endpoint and the
# legacy concierge agent's per-verb tools run through, so a visitor and the agent
# get identical validation and effects. Only VISITOR-scoped state (the visitor's
# own cart, a checkout link) auto-fires; every "gated" verb raises an Instinct
# proposal (decision_loop.propose_customer_action) and executes nothing. Checkout
# is a handoff LINK; the agent never runs payment.
#
#   execute_action(widget, workspace_id, customer_ref, verb, args, site=...):
#     * send_to_team (built in, reserved, never declared): the lead card's Send.
#       Reachable only when the caller passes the front gate's ``site`` (the public
#       route does; the agent's tool path doesn't), and only while the site's
#       ``concierge_lead_capture`` is on (409 otherwise). Fields name <=120,
#       email/phone (one required, contact_form checks), message <=2000; 3 per
#       visitor per 10 minutes, 30 per site per hour (429), counted off the
#       ``pawbar_lead`` marker. Writes a Lead via leads.capture_internal (HIGH
#       injection screen there) with conversation_ref "<widget_id>:<customer_ref>".
#       A field refusal is 422 with ``detail`` {code, field, message}; any other
#       422 has field None (paw-bar's lead form reads exactly this).
#     * declared verbs: every arg key declared, coerced to its flat type
#       (str/int/float/bool), strings capped at 256, qty clamped to 1..99;
#     * auto add_to_cart: the product must be in the widget's catalog; a cart holds
#       one currency (409 ``cart_currency_mismatch``, cart unchanged);
#     * auto checkout: renders checkout_url ({cart_ref} -> an opaque cart handle);
#       an empty cart is a 409;
#     * gated verb: raises the proposal (own per-visitor cap) and returns pending.
#   Returns an ``ActionOutcome`` the endpoint maps to HTTP and the tool to MCP.
#   Every action records a paw_bar event marker (audit + rate limit). A successful
#   auto verb or send_to_team records ``paw.visitor.action`` in the agent ledger
#   (never raises); amounts are ISO 4217 minor units. Failures record no ledger row.

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# The built-in lead verb. Reserved: ``PawBarActionSpec`` refuses it as an owner
# action, so it can only mean this.
SEND_TO_TEAM_VERB = "send_to_team"
# Marker type every send_to_team attempt that got past field checks records; the
# two lead caps count it.
LEAD_MARKER_TYPE = "pawbar_lead"
LEAD_FIELD_CAPS: dict[str, int] = {"name": 120, "email": 254, "phone": 40, "message": 2000}
LEADS_PER_VISITOR = 3
LEAD_VISITOR_WINDOW = timedelta(minutes=10)
LEADS_PER_SITE = 30
LEAD_SITE_WINDOW = timedelta(hours=1)
LEAD_SENT_MESSAGE = "Sent. The team will get back to you."
_LEAD_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_MAX_ARG_STR = 256
_MIN_QTY = 1
_MAX_QTY = 99


@dataclass
class ActionOutcome:
    """The executor's structured result — mapped to HTTP or MCP by the caller.

    ``ok`` is the success flag; ``result`` is the verb-specific payload; ``cart``
    is the visitor's cart summary (a JSON-safe dict) when the verb touched it;
    ``error`` is a stable machine code on failure; ``http_status`` is the status
    the endpoint should return (200 ok, 409 empty-cart/unavailable/
    cart_currency_mismatch, 422 bad verb/args, 429 a rate cap). ``detail``, when
    set, is the structured refusal the endpoint returns instead of ``error``
    ({code, field, message} for send_to_team). Amounts in ``cart`` are ISO 4217
    minor units of its ``currency``."""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    cart: dict[str, Any] | None = None
    error: str = ""
    http_status: int = 200
    detail: dict[str, Any] | None = None


def _fail(error: str, http_status: int) -> ActionOutcome:
    return ActionOutcome(ok=False, error=error, http_status=http_status)


def _coerce_arg(type_name: str, value: Any) -> tuple[bool, Any]:
    """Coerce one arg to its declared flat type. Returns (ok, coerced_value)."""
    try:
        if type_name == "str":
            return True, str(value)[:_MAX_ARG_STR]
        if type_name == "bool":
            if isinstance(value, bool):
                return True, value
            if isinstance(value, (int, float)):
                return True, bool(value)
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes"):
                    return True, True
                if low in ("false", "0", "no", ""):
                    return True, False
            return False, None
        if type_name == "int":
            # bool is a subclass of int — reject it as an int arg to avoid
            # True==1 confusion; require a real number/digit string.
            if isinstance(value, bool):
                return False, None
            return True, int(value)
        if type_name == "float":
            if isinstance(value, bool):
                return False, None
            return True, float(value)
    except (TypeError, ValueError):
        return False, None
    return False, None


def _validate_args(
    declared: dict[str, str], args: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """Validate + coerce visitor args against the action's declared arg types.

    Unknown keys (not in the declared map) are rejected. Each provided value is
    coerced to its declared flat type; a value that can't coerce is rejected.
    Returns ``(coerced, "")`` on success or ``(None, error_code)`` on failure.
    Missing declared args are allowed (the verb handler enforces what it needs).
    """
    if not isinstance(args, dict):
        return None, "args_not_object"
    coerced: dict[str, Any] = {}
    for key, raw in args.items():
        if key not in declared:
            return None, f"unknown_arg:{key}"
        ok, val = _coerce_arg(declared[key], raw)
        if not ok:
            return None, f"bad_arg_type:{key}"
        coerced[key] = val
    return coerced, ""


def _cart_ref(widget_id: str, customer_ref: str) -> str:
    """An opaque, non-reversible handle for a visitor's cart used in checkout_url.

    A deterministic hash of (widget_id, customer_ref) — the same cart always maps
    to the same ref (so a checkout page can correlate) without exposing the raw
    customer handle in the URL."""
    digest = hashlib.sha256(f"{widget_id}:{customer_ref}".encode()).hexdigest()
    return digest[:32]


def _cart_dict(cart: Any, *, checkout_url: str = "") -> dict[str, Any]:
    """Serialize a PawBarCart to the wire shape GET /paw-bar/cart returns."""
    return {
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "price_cents": item.price_cents,
                "qty": item.qty,
            }
            for item in cart.items
        ],
        "total_cents": cart.total_cents,
        "currency": cart.currency,
        "checkout_url": checkout_url,
    }


def cart_wire(widget: Any, customer_ref: str, cart: Any) -> dict[str, Any]:
    """The single {items,total_cents,currency,checkout_url} serializer.

    Used by BOTH the executor's cart-touching results and GET /paw-bar/cart, so
    the two never drift. ``cart`` may be ``None`` (no cart yet) → an empty cart
    with the widget's rendered checkout_url. The checkout_url has ``{cart_ref}``
    substituted with the opaque cart handle."""
    widget_id = str(getattr(widget, "id", "") or "")
    spec = getattr(widget, "spec", None)
    checkout_url = _render_checkout_url(
        str(getattr(spec, "checkout_url", "") or ""), widget_id, customer_ref
    )
    if cart is None:
        return {"items": [], "total_cents": 0, "currency": "USD", "checkout_url": checkout_url}
    return _cart_dict(cart, checkout_url=checkout_url)


# The gated-action marker type is FIXED (not verb-suffixed) so the dedicated
# gated rate cap can count it with a single equality filter. Auto actions keep a
# verb-suffixed type for a readable owner audit trail.
GATED_MARKER_TYPE = "pawbar_gated_action"
# A dedicated, lower cap for proposal-generating (gated) actions, separate from
# the widget's overall per-customer cap: rotating customer_ref must not flood the
# owner's Instinct tray at the full widget rate.
GATED_ACTIONS_PER_MIN = 6


async def _record_action_marker(
    store: Any, widget_id: str, customer_ref: str, verb: str, policy: str, ok: bool
) -> None:
    """Best-effort audit + rate-limit marker via the layer's event mechanism.

    Recording an event reuses the paw_bar layer's existing audit trail (owner
    reads it via recent_events) AND feeds the shared rate limiter, so a burst of
    actions is throttled like any other widget traffic. Gated actions use the
    FIXED ``pawbar_gated_action`` type so the dedicated gated cap can count them;
    auto actions use ``pawbar_action:<verb>``. A store hiccup must never fail the
    action."""
    try:
        from pocketpaw.paw_bar.models import PawBarEvent

        event_type = GATED_MARKER_TYPE if policy == "gated" else f"pawbar_action:{verb}"
        await store.record_event(
            PawBarEvent(
                widget_id=widget_id,
                type=event_type,
                payload={"policy": policy, "verb": verb, "ok": ok},
                customer_ref=customer_ref,
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("paw-bar action marker record failed (non-fatal)", exc_info=True)


async def execute_action(
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    verb: str,
    args: dict[str, Any],
    *,
    store: Any | None = None,
    site: Any | None = None,
) -> ActionOutcome:
    """Execute one Paw Bar action — the shared endpoint + tool code path.

    ``widget`` is the resolved :class:`PawBarWidget`; ``workspace_id`` is its
    resolved tenant (used to scope the gated Instinct proposal); ``customer_ref``
    is the anonymous visitor handle; ``verb`` / ``args`` are the requested action.
    ``site`` is the Site the public front gate resolved; only with it is the
    built-in send_to_team verb reachable. See the module header for the full
    contract. Never raises for a caller error — returns an :class:`ActionOutcome`
    with a stable code + status hint.
    """
    if store is None:
        from pocketpaw.stores import get_paw_bar_store

        store = get_paw_bar_store()

    if verb == SEND_TO_TEAM_VERB and site is not None:
        return await _do_send_to_team(store, widget, site, workspace_id, customer_ref, args)

    spec = getattr(widget, "spec", None)
    declared_actions = list(getattr(spec, "actions", []) or [])
    action = next((a for a in declared_actions if a.verb == verb), None)
    if action is None:
        return _fail("verb_not_declared", 422)

    coerced, arg_err = _validate_args(dict(action.args), args)
    if coerced is None:
        return _fail(arg_err, 422)

    policy = action.policy

    # --- auto (visitor-scoped) verbs: add_to_cart / checkout ---------------
    if policy == "auto":
        if verb == "add_to_cart":
            outcome = await _do_add_to_cart(store, widget, spec, customer_ref, coerced)
        elif verb == "checkout":
            outcome = await _do_checkout(store, widget, spec, customer_ref)
        else:
            # The spec validator forbids policy="auto" on any non-cart verb, so
            # this is unreachable for a validated spec — fail closed if it isn't.
            return _fail("unsupported_auto_verb", 422)
        # AL-2 — the visitor-action beat, recorded HERE rather than inside each
        # verb handler: one place means the two verbs cannot disagree about the
        # row's shape, and a third auto verb gets its row for free. Only a
        # SUCCESSFUL action is a beat — a rejected product id or an empty cart
        # changed nothing and belongs on no board. Fail-soft by construction.
        if outcome.ok:
            from pocketpaw_ee.paw_bar import ledger

            await ledger.emit_visitor_action(
                widget=widget,
                # The executor's resolved tenant — the same token the gated path
                # below routes its Instinct store by, so both halves of the
                # action registry write into one tenant's ledger file.
                workspace_id=workspace_id,
                customer_ref=customer_ref,
                verb=verb,
                spec=spec,
                result=outcome.result,
                cart=outcome.cart,
                store=store,
            )
        return outcome

    # --- gated verbs: the proposal is the ONLY effect (SS-2) ----------------
    return await _do_gated(store, widget, workspace_id, customer_ref, verb, coerced)


async def _do_add_to_cart(
    store: Any, widget: Any, spec: Any, customer_ref: str, args: dict[str, Any]
) -> ActionOutcome:
    from pocketpaw.paw_bar.models import PawBarCartItem
    from pocketpaw.paw_bar.store import CartCurrencyMismatch

    widget_id = str(getattr(widget, "id", "") or "")
    product_id = str(args.get("product_id", "") or "")
    if not product_id:
        return _fail("missing_product_id", 422)
    found = await store.get_catalog_items(widget_id, [product_id])
    product = found[0] if found else None
    if product is None:
        return _fail("unknown_product", 422)

    # qty defaults to 1 and is CLAMPED to [1, 99] (a cap, per the contract).
    qty_raw = args.get("qty", 1)
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        qty = 1
    qty = max(_MIN_QTY, min(_MAX_QTY, qty))

    item = PawBarCartItem(
        id=product.id,
        name=product.name,
        price_cents=product.price_cents,
        currency=product.currency,
        qty=qty,
    )
    try:
        cart = await store.upsert_cart_item(widget_id, customer_ref, item)
    except CartCurrencyMismatch as exc:
        # One currency per cart, so the total is always a real amount. The cart
        # is unchanged; the visitor checks out first or picks a same-currency item.
        logger.info(
            "paw_bar.action.refused verb=add_to_cart widget=%s product=%s cart=%s item=%s",
            widget_id,
            product_id,
            exc.cart_currency,
            exc.item_currency,
        )
        return ActionOutcome(
            ok=False,
            error=CartCurrencyMismatch.code,
            http_status=409,
            result={
                "message": (
                    f"Your cart is in {exc.cart_currency}, and this item is priced in "
                    f"{exc.item_currency}. Check out first, or pick items in "
                    f"{exc.cart_currency}."
                ),
                "cart_currency": exc.cart_currency,
                "item_currency": exc.item_currency,
            },
        )
    await _record_action_marker(store, widget_id, customer_ref, "add_to_cart", "auto", True)
    logger.info(
        "paw_bar.action.executed verb=add_to_cart widget=%s product=%s qty=%s",
        widget_id,
        product_id,
        qty,
    )
    return ActionOutcome(
        ok=True,
        result={"added": product.id, "qty": qty},
        cart=cart_wire(widget, customer_ref, cart),
    )


async def _do_checkout(store: Any, widget: Any, spec: Any, customer_ref: str) -> ActionOutcome:
    widget_id = str(getattr(widget, "id", "") or "")
    checkout_url = str(getattr(spec, "checkout_url", "") or "")
    if not checkout_url:
        return _fail("checkout_unavailable", 409)
    cart = await store.get_cart(widget_id, customer_ref)
    if cart is None or not cart.items:
        return _fail("empty_cart", 409)

    rendered = _render_checkout_url(checkout_url, widget_id, customer_ref)
    await _record_action_marker(store, widget_id, customer_ref, "checkout", "auto", True)
    logger.info(
        "paw_bar.action.executed verb=checkout widget=%s items=%s total=%s",
        widget_id,
        len(cart.items),
        cart.total_cents,
    )
    return ActionOutcome(
        ok=True,
        result={"checkout_url": rendered, "cart_ref": _cart_ref(widget_id, customer_ref)},
        cart=cart_wire(widget, customer_ref, cart),
    )


def _render_checkout_url(checkout_url: str, widget_id: str, customer_ref: str) -> str:
    """Substitute the ``{cart_ref}`` placeholder with the opaque cart handle."""
    if not checkout_url:
        return ""
    return checkout_url.replace("{cart_ref}", _cart_ref(widget_id, customer_ref))


async def _do_gated(
    store: Any,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    verb: str,
    args: dict[str, Any],
) -> ActionOutcome:
    from datetime import datetime, timedelta

    from pocketpaw_ee.paw_bar.decision_loop import propose_customer_action

    widget_id = str(getattr(widget, "id", "") or "")

    # Dedicated gated cap (proposal spam): count this customer's recent gated
    # actions and refuse before proposing once over the per-minute ceiling, so a
    # rotating customer_ref can't flood the owner's Instinct tray at the full
    # widget rate. Best-effort — a count error must not block a legitimate action.
    try:
        window = datetime.now() - timedelta(minutes=1)
        recent_gated = await store.count_events_since(
            widget_id, window, customer_ref=customer_ref, event_type=GATED_MARKER_TYPE
        )
        if recent_gated >= GATED_ACTIONS_PER_MIN:
            return _fail("gated_rate_limit", 429)
    except Exception:  # noqa: BLE001
        logger.debug("gated-action rate check failed (allowing)", exc_info=True)

    summary = ", ".join(f"{k}={v}" for k, v in sorted(args.items())) or "(no args)"
    action_id = await propose_customer_action(
        widget=widget,
        workspace_id=workspace_id,
        customer_ref=customer_ref,
        verb=verb,
        args=args,
        summary=summary,
        paw_bar_store=store,
    )
    await _record_action_marker(
        store, widget_id, customer_ref, verb, "gated", action_id is not None
    )
    logger.info(
        "paw_bar.action.proposed verb=%s widget=%s instinct_action=%s",
        verb,
        widget_id,
        action_id,
    )
    if action_id is None:
        # The proposal could not be raised (owner-less widget / transient store
        # error). Nothing executed — tell the visitor we couldn't take it.
        return _fail("action_not_available", 409)
    return ActionOutcome(
        ok=True,
        result={
            "status": "pending",
            "instinct_action_id": action_id,
            "message": "Your request was sent to the team for review.",
        },
    )


# --------------------------------------------------------------------------- #
# send_to_team — the lead card's Send
# --------------------------------------------------------------------------- #


def _lead_refusal(code: str, message: str, field_name: str | None = None) -> ActionOutcome:
    """A 422 in the shape paw-bar's lead form reads: ``{code, field, message}``.
    ``field`` names one of the form's fields, or is None for a generic refusal."""
    return ActionOutcome(
        ok=False,
        error=code,
        http_status=422,
        detail={"code": code, "field": field_name, "message": message},
    )


def _lead_fields(args: Any) -> tuple[dict[str, str] | None, ActionOutcome | None]:
    """Validate the lead card's args; ``(fields, None)`` or ``(None, refusal)``.
    Empty values are dropped (the client omits them too)."""
    from pocketpaw.sites_capture.contact_form import looks_like_email, looks_like_phone

    if not isinstance(args, dict):
        return None, _lead_refusal("bad_request", "Something went wrong. Please try again.")
    fields: dict[str, str] = {}
    for name, raw in args.items():
        cap = LEAD_FIELD_CAPS.get(name)
        if cap is None:
            return None, _lead_refusal("unknown_field", "Something went wrong. Please try again.")
        if not isinstance(raw, str):
            return None, _lead_refusal("not_text", "Enter text here.", name)
        value = _LEAD_CONTROL_CHARS.sub("", raw).strip()
        if len(value) > cap:
            return None, _lead_refusal("too_long", f"Keep this under {cap} characters.", name)
        if value:
            fields[name] = value
    email, phone = fields.get("email"), fields.get("phone")
    if email and not looks_like_email(email):
        return None, _lead_refusal("invalid_email", "Enter a valid email address.", "email")
    if phone and not looks_like_phone(phone):
        return None, _lead_refusal("invalid_phone", "Enter a valid phone number.", "phone")
    if not email and not phone:
        return None, _lead_refusal(
            "contact_required", "Add an email or phone number so the team can reply.", "email"
        )
    return fields, None


async def _within_lead_rate(store: Any, widget_id: str, customer_ref: str) -> bool:
    """3 per visitor per 10 minutes, 30 per site (its widget) per hour, counted
    off the lead marker. A counting failure ALLOWS the lead: losing one is the
    worse failure, and the front gate's per-minute caps still apply."""
    now = datetime.now()
    try:
        mine = await store.count_events_since(
            widget_id,
            now - LEAD_VISITOR_WINDOW,
            customer_ref=customer_ref,
            event_type=LEAD_MARKER_TYPE,
        )
        if mine >= LEADS_PER_VISITOR:
            return False
        site_total = await store.count_events_since(
            widget_id, now - LEAD_SITE_WINDOW, event_type=LEAD_MARKER_TYPE
        )
        return site_total < LEADS_PER_SITE
    except Exception:  # noqa: BLE001
        logger.debug("lead rate check failed (allowing)", exc_info=True)
        return True


async def _record_lead_marker(store: Any, widget_id: str, customer_ref: str, ok: bool) -> None:
    try:
        from pocketpaw.paw_bar.models import PawBarEvent

        await store.record_event(
            PawBarEvent(
                widget_id=widget_id,
                type=LEAD_MARKER_TYPE,
                payload={"policy": "builtin", "verb": SEND_TO_TEAM_VERB, "ok": ok},
                customer_ref=customer_ref,
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("paw-bar lead marker record failed (non-fatal)", exc_info=True)


async def _do_send_to_team(
    store: Any,
    widget: Any,
    site: Any,
    workspace_id: str,
    customer_ref: str,
    args: Any,
) -> ActionOutcome:
    from pocketpaw_ee.cloud.leads import service as leads_service
    from pocketpaw_ee.paw_bar import ledger

    widget_id = str(getattr(widget, "id", "") or "")
    if getattr(site, "concierge_lead_capture", True) is False:
        return _fail("lead_capture_off", 409)
    fields, refusal = _lead_fields(args)
    if refusal is not None:
        return refusal
    if not await _within_lead_rate(store, widget_id, customer_ref):
        return _fail("lead_rate_limit", 429)
    try:
        lead = await leads_service.capture_internal(
            site=site,
            form_type="concierge",
            kind="concierge",
            properties=fields,
            conversation_ref=ledger.conversation_id(widget_id, customer_ref),
        )
    except Exception:  # noqa: BLE001 — a store error is a retry, never a 500
        logger.warning("send_to_team lead write failed for widget %s", widget_id, exc_info=True)
        return _fail("lead_unavailable", 503)
    # Counted whether or not the screen dropped it, so a visitor can't probe the
    # screen faster than the cap allows.
    await _record_lead_marker(store, widget_id, customer_ref, lead is not None)
    if lead is None:
        return _lead_refusal("rejected", "We couldn't send that. Please reword it and try again.")
    logger.info("paw_bar.action.executed verb=send_to_team widget=%s lead=%s", widget_id, lead.id)
    result = {"message": LEAD_SENT_MESSAGE}
    await ledger.emit_visitor_action(
        widget=widget,
        workspace_id=workspace_id,
        customer_ref=customer_ref,
        verb=SEND_TO_TEAM_VERB,
        spec=getattr(widget, "spec", None),
        result=result,
        store=store,
    )
    return ActionOutcome(ok=True, result=result)


__all__ = [
    "LEAD_MARKER_TYPE",
    "SEND_TO_TEAM_VERB",
    "ActionOutcome",
    "cart_wire",
    "execute_action",
]
