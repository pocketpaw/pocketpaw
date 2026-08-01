# ee/paw_bar/ledger.py — the paw-bar half of the agent ledger (AL-2).
# Created: 2026-08-01 (AL-2, paw-bar emitters) — AL-1 gave every Instinct-gated
#   beat a row. The rest of the concierge funnel had none: a visitor started a
#   conversation, filled a cart, asked for a human, got an answer delivered, and
#   the ledger saw only the middle of it. This module is the other four fifths —
#   the beats the Cedar & Stone demo flow actually consists of:
#
#     paw.conversation.started   a new person started talking to this agent
#     paw.visitor.action         add_to_cart / checkout, with attributed value
#     paw.handoff.raised         someone asked for a person
#     paw.action.delivered       the decided answer reached the visitor
#     paw.conversation.takeover  the owner took the conversation off the agent
#     paw.handoff.resolved       the owner finished what the agent handed over
#
#   WHY ONE MODULE INSTEAD OF FIVE INLINE EMITTERS. Every beat below has to get
#   four things right — the kind, the ref, the agent, and WHICH FILE the row
#   lands in — and three of those four are the kind of decision that fails
#   silently. Spreading them across decision_loop / handoff / actions / router
#   means four independent chances to spell the funnel differently. The producers
#   keep their call sites (a reader of decision_loop still sees the delivered
#   emit fire there); what lives here is the row SHAPE, one file, reviewable in
#   one screen. Same reasoning that put the kind vocabulary in one models module.
#
#   FAIL-SOFT IS THE CONTRACT, and here it is mechanical: every public emitter
#   carries ``@_never_raises``, so a bookkeeping failure can never reach a
#   visitor's turn, an owner's click, or an agent's tool call. That is the same
#   rule as ``paw_bar/notify.py`` and ``instinct/store.py::_emit_ledger``, and it
#   is the reason the reconcile endpoint (AL-4) exists: silence is the price of
#   never costing anyone their answer, and a drift alarm is how we pay it back.
#
#   WORKSPACE ROUTING — the one that bites. Two DIFFERENT values, deliberately
#   never conflated (the decision_loop header tells the full story):
#     * ``store_workspace_id`` picks the FILE
#       (``~/.pocketpaw/workspaces/<id>/agent_ledger.db``). It must be the real,
#       server-stamped workspace token — the same value the surrounding code
#       routes its instinct/fabric store by. Feeding it the widget OWNER label
#       (``user:maya``) makes the path allowlist raise inside the guard and the
#       row vanishes without a trace.
#     * ``workspace_id`` is the IN-ROW tenancy column the read endpoint filters
#       by. It is whatever the surrounding beat is already scoped by, so an
#       AL-2 row lands in the same bucket as the AL-1 row for the same action.
#   Both are REQUIRED keyword arguments with no defaults: a caller that has not
#   thought about the difference cannot accidentally inherit one for the other.
#
#   NEVER on a row: tokens, cost, latency, model mix. Ops stays federated (see
#   the agent_ledger models header) — the row model has no field for them.

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# How many characters of an ISO-8601 stamp identify the SECOND ("2026-08-01T
# 09:14:22"). Episode refs truncate here on purpose — see ``_episode_stamp``.
_ISO_SECONDS = 19


def _never_raises(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap an emitter so a bookkeeping failure can never reach the caller.

    The decorator IS the contract, applied uniformly instead of six hand-written
    try/excepts that can each drift or be forgotten by the seventh emitter. It
    swallows everything — a store that cannot resolve its workspace (a real
    ``WorkspaceScopeRequired`` in cloud mode), a locked database, a vocabulary
    rejection, a duck-typed widget from a test — logs at debug, and returns
    ``False`` so a caller that cares can tell "recorded" from "not recorded"
    without ever being forced to handle it.
    """

    @functools.wraps(fn)
    async def _guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 — bookkeeping never breaks the caller
            logger.debug("agent-ledger emit skipped in %s", fn.__name__, exc_info=True)
            return False

    return _guarded


def conversation_id(widget_id: str, customer_ref: str) -> str:
    """The composite id one paw-bar conversation is known by everywhere.

    ``(widget_id, customer_ref)`` is the identity the conversations table, the
    transcript endpoint, and the notification source id are all keyed by, so the
    ledger uses the same pair rather than minting a third handle. It rides in
    ``attrs`` under the OTel ``gen_ai.conversation.id`` name, which is exactly
    what it is.
    """
    return f"{widget_id}:{customer_ref}"


def _episode_stamp(value: Any = None) -> str:
    """A UTC ISO stamp truncated to the SECOND — the episode half of a ref.

    Several beats can legitimately repeat over one conversation's life (a second
    handoff, a second takeover after the bot auto-resumed, a second cart add), so
    their refs cannot be the conversation alone or the ledger would silently drop
    the repeat as a replay. They pin the moment instead.

    Truncated to the second rather than the microsecond deliberately: at
    microsecond resolution every ref is unique and ``UNIQUE(kind, ref)`` stops
    absorbing anything, so a double-submitted click would double-count — and for
    a cart add that means double-counting MONEY. One second is the window in
    which two identical beats are a duplicate rather than a repeat.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="seconds")
    text = str(value or "").strip()
    if text:
        return text[:_ISO_SECONDS]
    return datetime.now(UTC).isoformat(timespec="seconds")


def _widget_agent(widget: Any) -> str:
    """The agent bound to this widget, via the EXISTING fail-soft resolver.

    Imported lazily from ``decision_loop`` (which owns it and whose propose paths
    stamp the same value onto the Instinct Action) so the whole funnel attributes
    to one agent id, and so this module and ``decision_loop`` do not import each
    other at module level.
    """
    from pocketpaw_ee.paw_bar.decision_loop import resolve_widget_agent

    return resolve_widget_agent(widget)


async def _append(
    *,
    kind: str,
    ref: str,
    agent_id: str,
    workspace_id: str,
    store_workspace_id: str,
    actor: str,
    attrs: dict[str, Any],
    value_cents: int | None = None,
    currency: str | None = None,
) -> bool:
    """Build one row and append it. UNGUARDED — every caller is decorated.

    Returns True when a NEW row landed; False means ``(kind, ref)`` was already
    recorded, which is a replay rather than a failure.

    No ``outcome`` parameter: none of the paw-bar beats carries a verdict. A
    cart add is not "solved", and whether the delivered answer actually helped is
    the separate question ``paw.action.outcome`` exists for (issue #1162's
    distinction between an output and an outcome). Leaving the field unset keeps
    those rows out of the solved-ratio denominator, where they would silently
    depress every agent's score.

    The store comes from the workspace-keyed factory (never a direct
    construction — see tests/test_store_isolation_lint.py) and an empty
    ``store_workspace_id`` is passed as ``None``, which is the legacy shared
    ``~/.pocketpaw/agent_ledger.db`` on a single-tenant box and a deliberate
    fail-closed raise on a cloud one.
    """
    from pocketpaw.agent_ledger.models import SURFACE_PAW_BAR, LedgerRow
    from pocketpaw.stores import get_agent_ledger_store

    row = LedgerRow(
        agent_id=agent_id,
        workspace_id=workspace_id,
        surface=SURFACE_PAW_BAR,
        kind=kind,
        value_cents=value_cents,
        currency=currency,
        ref=ref,
        actor=actor,
        attrs=attrs,
    )
    store = get_agent_ledger_store(workspace_id=store_workspace_id or None)
    return await store.append(row)


def _base_attrs(widget: Any, customer_ref: str, agent_id: str) -> dict[str, Any]:
    """The attributes every paw-bar beat carries, under the governed names."""
    from pocketpaw.agent_ledger.models import (
        ATTR_AGENT_ID,
        ATTR_CONVERSATION_ID,
        ATTR_POCKET_ID,
        ATTR_WIDGET_ID,
    )

    widget_id = str(getattr(widget, "id", "") or "")
    attrs: dict[str, Any] = {
        ATTR_WIDGET_ID: widget_id,
        ATTR_CONVERSATION_ID: conversation_id(widget_id, customer_ref),
    }
    pocket_id = str(getattr(widget, "pocket_id", "") or "")
    if pocket_id:
        attrs[ATTR_POCKET_ID] = pocket_id
    if agent_id:
        # Mirrors the Instinct emitter: the column is the key, the attribute is
        # the copy, and an unattributed row simply omits it rather than carrying
        # an empty string that reads like a real id.
        attrs[ATTR_AGENT_ID] = agent_id
    return attrs


# ---------------------------------------------------------------------------
# Emitter #2 — the delivered beat (decision_loop.deliver_customer_decision)
# ---------------------------------------------------------------------------


@_never_raises
async def emit_action_delivered(
    *,
    action: Any,
    widget: Any,
    customer_ref: str,
    row_workspace_id: str,
    decided_by: str,
) -> bool:
    """The approved answer actually reached the person waiting for it.

    ``ref`` is the INSTINCT ACTION ID — the identity of the decision itself, and
    the same ref AL-1's approved row carries, so the funnel joins on one key and
    a re-delivery (an approve replay, a later sweep re-resolving the parked row)
    is absorbed by ``UNIQUE(kind, ref)`` instead of counting twice.

    Attribution prefers the Action's own ``actor_agent_id`` — stamped at propose
    time from this very widget — so the delivered row can never disagree with the
    approved row about whose ledger this belongs on. The widget lookup is the
    fallback for an Action proposed before AL-1 shipped.

    ``row_workspace_id`` is the blob's workspace (the Action's in-row scope), and
    the FILE is routed by the widget's real ``workspace_id`` — the same token
    that routed the instinct.db this action was proposed into. Never the blob's
    value: for a legacy widget that is the owner label, and the factory would
    raise on it inside the guard, dropping the row silently.
    """
    from pocketpaw.agent_ledger.models import (
        ATTR_ACTION_ID,
        ATTR_DECISION_ACTOR,
        KIND_ACTION_DELIVERED,
        LedgerActor,
    )

    agent_id = str(getattr(action, "actor_agent_id", "") or "") or _widget_agent(widget)
    attrs = _base_attrs(widget, customer_ref, agent_id)
    action_id = str(getattr(action, "id", "") or "")
    attrs[ATTR_ACTION_ID] = action_id
    # WHO decided rides as an attribute; the row's ``actor`` is SYSTEM because
    # the delivery itself is machinery — the human's click is already counted on
    # AL-1's approved row, and counting it twice as a human act would inflate the
    # "a person did this" half of the board.
    attrs[ATTR_DECISION_ACTOR] = decided_by

    return await _append(
        kind=KIND_ACTION_DELIVERED,
        ref=action_id,
        agent_id=agent_id,
        workspace_id=row_workspace_id,
        store_workspace_id=str(getattr(widget, "workspace_id", "") or ""),
        actor=LedgerActor.SYSTEM.value,
        attrs=attrs,
    )


# ---------------------------------------------------------------------------
# Emitter #3 — the handoff beats (paw_bar/handoff.py + the owner's inbox)
# ---------------------------------------------------------------------------


@_never_raises
async def emit_handoff_raised(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    handoff_id: str = "",
    source: str = "visitor",
) -> bool:
    """A visitor (or the concierge on their behalf) asked for a person.

    ``ref`` is the Fabric handoff object's id when one was written — the natural
    identity of the record the owner reads — falling back to
    ``widget:customer:<second>`` when only the queue flip landed (a handoff with
    no object is still a real ask; see the handoff module's partial-failure
    contract). NOT the conversation alone: a visitor may legitimately ask twice,
    and collapsing the second ask into the first would under-count exactly the
    signal an owner most wants to see.

    ``actor`` distinguishes the visitor pressing the button from the agent
    escalating itself — the same row, two very different stories about autonomy.
    """
    from pocketpaw.agent_ledger.models import (
        ATTR_HANDOFF_SOURCE,
        KIND_HANDOFF_RAISED,
        LedgerActor,
    )

    agent_id = _widget_agent(widget)
    attrs = _base_attrs(widget, customer_ref, agent_id)
    attrs[ATTR_HANDOFF_SOURCE] = source

    widget_id = str(getattr(widget, "id", "") or "")
    ref = handoff_id or f"{conversation_id(widget_id, customer_ref)}:{_episode_stamp()}"
    actor = LedgerActor.AGENT.value if source == "agent" else LedgerActor.VISITOR.value

    return await _append(
        kind=KIND_HANDOFF_RAISED,
        ref=ref,
        agent_id=agent_id,
        workspace_id=workspace_id,
        store_workspace_id=workspace_id,
        actor=actor,
        attrs=attrs,
    )


@_never_raises
async def emit_handoff_resolved(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    at: Any = None,
) -> bool:
    """The owner finished what the agent handed over.

    Fires when a conversation LEAVES ``needs_human`` — which is the only thing in
    the product that means "a person dealt with it". ``ref`` pins the resolving
    moment (``widget:customer:<second>``) because a conversation can be escalated
    and resolved repeatedly; a replayed identical patch inside the same second is
    a duplicate and collapses.
    """
    from pocketpaw.agent_ledger.models import KIND_HANDOFF_RESOLVED, LedgerActor

    agent_id = _widget_agent(widget)
    widget_id = str(getattr(widget, "id", "") or "")

    return await _append(
        kind=KIND_HANDOFF_RESOLVED,
        ref=f"{conversation_id(widget_id, customer_ref)}:{_episode_stamp(at)}",
        agent_id=agent_id,
        workspace_id=workspace_id,
        store_workspace_id=workspace_id,
        actor=LedgerActor.OWNER.value,
        attrs=_base_attrs(widget, customer_ref, agent_id),
    )


# ---------------------------------------------------------------------------
# Emitter #4 — the visitor's own actions (paw_bar/actions.py auto verbs)
# ---------------------------------------------------------------------------


def _catalog_value(spec: Any, result: dict[str, Any]) -> tuple[int | None, str | None, str]:
    """``(value_cents, currency, product_id)`` for one add_to_cart, from the SPEC.

    Priced off the widget spec's catalog row — the owner's declared price — and
    multiplied by the quantity that was actually added, because the cart merges
    quantities and the row should say what THIS add was worth. A product that is
    not in the catalog cannot be priced, and an unpriced row is the honest answer
    (the executor already refuses unknown products, so this is belt and braces).
    """
    product_id = str(result.get("added", "") or "")
    if not product_id:
        return None, None, ""
    catalog = {item.id: item for item in (getattr(spec, "catalog", []) or [])}
    product = catalog.get(product_id)
    if product is None:
        return None, None, product_id
    try:
        qty = max(1, int(result.get("qty", 1)))
    except (TypeError, ValueError):
        qty = 1
    return int(product.price_cents) * qty, str(product.currency or "USD"), product_id


@_never_raises
async def emit_visitor_action(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    verb: str,
    spec: Any,
    result: dict[str, Any] | None = None,
    cart: dict[str, Any] | None = None,
) -> bool:
    """One auto (visitor-scoped) verb the agent executed directly.

    Value attribution, v1 and deliberately RAW (design open question 1):
      * ``add_to_cart`` — the catalog price of the added product × the quantity
        added, so the add_to_cart rows for a visitor sum to their cart.
      * ``checkout``    — the cart total the checkout link was rendered for.

    CONSUMER WARNING, because ``value_by_currency`` sums every row it is given:
    a cart add and the checkout that follows it describe the SAME money. A
    headline "value attributed" figure must pick one lens (checkout rows are the
    conservative one) rather than adding both — summing them is a two-meters bug
    of exactly the kind this design was built to avoid.

    ``actor`` is VISITOR for both callers of the executor. The public endpoint is
    literally the visitor; the concierge's per-verb tool acts on the visitor's
    own state at their request, which is what the kind means ("a visitor-owned
    action the agent executed directly"). The executor cannot tell its two
    callers apart today, and a guess dressed as data is worse than the honest
    label the vocabulary already defines.
    """
    from pocketpaw.agent_ledger.models import (
        ATTR_PRODUCT_ID,
        ATTR_VISITOR_VERB,
        KIND_VISITOR_ACTION,
        LedgerActor,
    )

    agent_id = _widget_agent(widget)
    attrs = _base_attrs(widget, customer_ref, agent_id)
    attrs[ATTR_VISITOR_VERB] = verb

    widget_id = str(getattr(widget, "id", "") or "")
    value_cents: int | None = None
    currency: str | None = None
    # The ref pins (conversation, verb, thing, second). The PRODUCT is in there
    # because an agent can add two different products inside one turn — and one
    # second — and a ref without it would drop the second add along with its
    # money.
    ref_parts = [conversation_id(widget_id, customer_ref), verb]

    if verb == "add_to_cart":
        value_cents, currency, product_id = _catalog_value(spec, result or {})
        if product_id:
            ref_parts.append(product_id)
            attrs[ATTR_PRODUCT_ID] = product_id
    elif verb == "checkout" and cart:
        value_cents = int(cart.get("total_cents") or 0)
        currency = str(cart.get("currency") or "USD")

    ref_parts.append(_episode_stamp())

    return await _append(
        kind=KIND_VISITOR_ACTION,
        ref=":".join(ref_parts),
        agent_id=agent_id,
        workspace_id=workspace_id,
        store_workspace_id=workspace_id,
        actor=LedgerActor.VISITOR.value,
        attrs=attrs,
        value_cents=value_cents,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# Emitter #5 — the conversation beats (the paw-bar conversations write path)
# ---------------------------------------------------------------------------


@_never_raises
async def emit_conversation_started(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
) -> bool:
    """A new person started talking to this agent.

    ``ref`` is the conversation itself (``widget:customer``), which makes the
    "new" in the kind a property of the DATABASE rather than of the caller: this
    may be fired on every visitor turn and ``UNIQUE(kind, ref)`` keeps exactly
    the first. That is deliberately more robust than trusting a read-before-write
    "is this new" flag, which is exactly the sort of thing that quietly changes
    meaning during a refactor and turns one conversation into forty.
    """
    from pocketpaw.agent_ledger.models import KIND_CONVERSATION_STARTED, LedgerActor

    agent_id = _widget_agent(widget)
    widget_id = str(getattr(widget, "id", "") or "")

    return await _append(
        kind=KIND_CONVERSATION_STARTED,
        ref=conversation_id(widget_id, customer_ref),
        agent_id=agent_id,
        workspace_id=workspace_id,
        store_workspace_id=workspace_id,
        actor=LedgerActor.VISITOR.value,
        attrs=_base_attrs(widget, customer_ref, agent_id),
    )


@_never_raises
async def emit_conversation_takeover(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    at: Any = None,
) -> bool:
    """The owner took the conversation off the agent.

    ``ref`` is the conversation plus the moment the mute went on
    (``bot_paused_at``, which the store stamps on the flip), because takeover is
    an EPISODE: the bot hands itself back after the idle window and the owner can
    take it again tomorrow. Two episodes are two rows; one episode written twice
    is one row.
    """
    from pocketpaw.agent_ledger.models import KIND_CONVERSATION_TAKEOVER, LedgerActor

    agent_id = _widget_agent(widget)
    widget_id = str(getattr(widget, "id", "") or "")

    return await _append(
        kind=KIND_CONVERSATION_TAKEOVER,
        ref=f"{conversation_id(widget_id, customer_ref)}:{_episode_stamp(at)}",
        agent_id=agent_id,
        workspace_id=workspace_id,
        store_workspace_id=workspace_id,
        actor=LedgerActor.OWNER.value,
        attrs=_base_attrs(widget, customer_ref, agent_id),
    )


@_never_raises
async def emit_conversation_transition(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    before: Any,
    after: Any,
) -> bool:
    """Record whichever beats a conversation write actually crossed.

    ONE call at each owner-side write path, rather than a pair of conditionals
    copy-pasted into every handler that can mutate a conversation. Two
    transitions matter, and both are diffs, not events — which is why they are
    read from the before/after rows instead of from what the caller meant:

      * ``bot_paused`` OFF → ON              = takeover.
      * state ``needs_human`` → anything else = the handoff was resolved.

    A write that crosses neither records nothing. A write that crosses both (an
    owner who replies to an escalated thread and files it in one patch) records
    both, because they are two different things the owner did.
    """
    from pocketpaw.paw_bar.models import ConversationState

    if before is None or after is None:
        return False

    landed = False
    if not getattr(before, "bot_paused", False) and getattr(after, "bot_paused", False):
        landed |= bool(
            await emit_conversation_takeover(
                widget=widget,
                workspace_id=workspace_id,
                customer_ref=customer_ref,
                at=getattr(after, "bot_paused_at", "") or getattr(after, "updated_at", None),
            )
        )

    was_escalated = getattr(before, "state", None) == ConversationState.NEEDS_HUMAN
    still_escalated = getattr(after, "state", None) == ConversationState.NEEDS_HUMAN
    if was_escalated and not still_escalated:
        landed |= bool(
            await emit_handoff_resolved(
                widget=widget,
                workspace_id=workspace_id,
                customer_ref=customer_ref,
                at=getattr(after, "updated_at", None),
            )
        )
    return landed


__all__ = [
    "conversation_id",
    "emit_action_delivered",
    "emit_conversation_started",
    "emit_conversation_takeover",
    "emit_conversation_transition",
    "emit_handoff_raised",
    "emit_handoff_resolved",
    "emit_visitor_action",
]
