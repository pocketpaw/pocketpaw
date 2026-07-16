# ee/paw_bar/actions.py — the shared Paw Bar action executor (C1).
# Created: 2026-07-16 (Paw Bar action registry, C1) — the SINGLE code path both
#   the public POST /paw-bar/action endpoint and the concierge agent's per-verb
#   tools run through, so a visitor and the agent get identical validation +
#   effects. SS-2 alignment: agent-facing action tools NEVER execute tenant-scoped
#   effects. Only VISITOR-scoped state (the visitor's own cart / a handoff link)
#   auto-fires; every "gated" verb emits an Instinct proposal (via
#   decision_loop.propose_customer_action) and executes NOTHING. Checkout is a
#   handoff LINK — the agent never runs payment.
#
#   execute_action(widget, workspace_id, customer_ref, verb, args):
#     * validates the verb is declared on the spec, that every arg key is declared,
#       coerces each arg to its declared flat type (str/int/float/bool), caps
#       string args at 256 chars and clamps qty to 1..99;
#     * auto + add_to_cart: the product_id must exist in the catalog; upserts the
#       visitor's cart and returns the updated cart summary;
#     * auto + checkout: renders checkout_url ({cart_ref} → an opaque, non-
#       reversible cart handle); an empty cart returns a 409-style error;
#     * gated verb: raises an Instinct proposal (the only effect) and returns a
#       pending outcome the visitor polls on the existing decision endpoint.
#   Returns a plain ``ActionOutcome`` (no FastAPI/MCP coupling); the endpoint maps
#   it to HTTP, the tool maps it to an MCP result. Every executed/proposed action
#   is recorded as a paw_bar event marker (the layer's existing audit + rate-limit
#   mechanism) plus a structured log line.

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ARG_STR = 256
_MIN_QTY = 1
_MAX_QTY = 99


@dataclass
class ActionOutcome:
    """The executor's structured result — mapped to HTTP or MCP by the caller.

    ``ok`` is the success flag; ``result`` is the verb-specific payload; ``cart``
    is the visitor's cart summary (a JSON-safe dict) when the verb touched it;
    ``error`` is a stable machine code on failure; ``http_status`` is the status
    the endpoint should return (200 ok, 409 empty-cart/unavailable, 422 bad
    verb/args)."""

    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    cart: dict[str, Any] | None = None
    error: str = ""
    http_status: int = 200


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


async def _record_action_marker(
    store: Any, widget_id: str, customer_ref: str, verb: str, policy: str, ok: bool
) -> None:
    """Best-effort audit + rate-limit marker via the layer's event mechanism.

    Recording a ``pawbar_action:<verb>`` event reuses the paw_bar layer's existing
    audit trail (owner reads it via recent_events) AND feeds the shared rate
    limiter, so a burst of actions is throttled like any other widget traffic. A
    store hiccup must never fail the action."""
    try:
        from pocketpaw.paw_bar.models import PawBarEvent

        await store.record_event(
            PawBarEvent(
                widget_id=widget_id,
                type=f"pawbar_action:{verb}",
                payload={"policy": policy, "ok": ok},
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
) -> ActionOutcome:
    """Execute one declared Paw Bar action — the shared endpoint + tool code path.

    ``widget`` is the resolved :class:`PawBarWidget`; ``workspace_id`` is its
    resolved tenant (used to scope the gated Instinct proposal); ``customer_ref``
    is the anonymous visitor handle; ``verb`` / ``args`` are the requested action.
    See the module header for the full contract. Never raises for a caller error —
    returns an :class:`ActionOutcome` with a stable code + status hint.
    """
    if store is None:
        from pocketpaw.stores import get_paw_bar_store

        store = get_paw_bar_store()

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
            return await _do_add_to_cart(store, widget, spec, customer_ref, coerced)
        if verb == "checkout":
            return await _do_checkout(store, widget, spec, customer_ref)
        # The spec validator forbids policy="auto" on any non-cart verb, so this
        # is unreachable for a validated spec — fail closed if it ever isn't.
        return _fail("unsupported_auto_verb", 422)

    # --- gated verbs: the proposal is the ONLY effect (SS-2) ----------------
    return await _do_gated(store, widget, workspace_id, customer_ref, verb, coerced)


async def _do_add_to_cart(
    store: Any, widget: Any, spec: Any, customer_ref: str, args: dict[str, Any]
) -> ActionOutcome:
    from pocketpaw.paw_bar.models import PawBarCartItem

    widget_id = str(getattr(widget, "id", "") or "")
    product_id = str(args.get("product_id", "") or "")
    if not product_id:
        return _fail("missing_product_id", 422)
    catalog = {item.id: item for item in (getattr(spec, "catalog", []) or [])}
    product = catalog.get(product_id)
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
    cart = await store.upsert_cart_item(widget_id, customer_ref, item)
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
    from pocketpaw_ee.paw_bar.decision_loop import propose_customer_action

    widget_id = str(getattr(widget, "id", "") or "")
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


__all__ = ["ActionOutcome", "cart_wire", "execute_action"]
