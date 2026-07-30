# ee/paw_bar/models.py — Pydantic models for the Paw Bar widget layer.
# Updated: 2026-07-30 (async decision delivery) — DecisionStatus gains an
#   optional ``contact_email`` (empty default). A visitor who leaves the page
#   while their request is PENDING can leave an email; when the owner decides,
#   the delivery hook sends the same customer-facing reply there. PII posture:
#   the email lives ONLY on this row — see the field comment.
# Updated: 2026-07-16 (Paw Bar action registry, C1) — the visitor-commerce
#   vocabulary. PawBarSpec gains three optional fields: ``actions`` (declared
#   verbs: {verb, policy in {auto,gated}, args flat-type-map, label}), ``catalog``
#   (products: {id, name, price_cents>=0, currency, image_url, url}) and
#   ``checkout_url`` (http(s), may carry a ``{cart_ref}`` placeholder). New
#   validators reject a malformed declaration with a clear error (unique
#   snake_case verbs, policy allowlist, flat arg types, unique catalog ids,
#   non-negative int prices, http(s) checkout url). ``PawBarCartItem`` /
#   ``PawBarCart`` are the visitor-scoped cart the store persists per
#   (widget_id, customer_ref). All additive — a spec with none of these fields
#   is byte-identical to today. SS-2 alignment lives in the executor, not here.
# Updated: 2026-07-14 (Paw Bar concierge seam, T3) — PawBarWidget +
#   PawBarWidgetPublic gain `agent_id: str = ""`, mirroring the workspace_id
#   column right beside it (same nullability/default). It binds a concierge
#   widget to the agent that answers its chats; "" = unbound (legacy / no agent).
#   The KB scope is NOT stored — it is derived as `pocket:<pocket_id>` where
#   needed — so this is the only new tenancy-adjacent field.
# Updated: 2026-07-11 (W4a tenancy seam) — PawBarWidget + PawBarWidgetPublic
#   gain `workspace_id: str = ""` (in-row tenancy, same model as DecisionStatus).
#   Empty string = legacy/single-tenant row; the store's scoped reads match it.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (PawPrint*→PawBar* models).
#   The separate one-word audit feed (past-tense record) is a DIFFERENT feature, untouched.
# Created: 2026-04-13 (Move 3 PR-A) — Minimal, secure-by-design render vocabulary
# (text / image / list / button / form / divider). No raw HTML, no script
# injection paths. The widget.js bundle consumes PawBarSpec; the backend
# consumes PawBarEvent on the ingest side.
# Updated: 2026-06-10 (W0b security fix) — Added PawBarWidgetPublic, a
# token-free projection of PawBarWidget used as the response model for
# list/read endpoints so the per-widget access_token never leaves the server
# in those payloads. The token is now only returned by the explicit,
# authenticated create + rotate-token paths.
# Updated: 2026-06-11 (gap2 — close the customer decision loop) — Added
# DecisionStatus + DecisionState. An inbound customer event no longer dead-ends
# at a Fabric object: it can raise an Instinct proposal, and the human's
# decision (reply text + state) is recorded here as the deliverable the
# customer surface polls back. DecisionStatus is keyed by (widget_id,
# customer_ref) so a rendered widget can fetch "what did the owner decide about
# my request?" without any owner credential. State machine: pending → delivered
# (approved) | declined (rejected). This is the back-half of the loop the
# module docstring promised since 2026-04-13 but never wired.

from __future__ import annotations

import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from pocketpaw.fabric.models import _gen_id

_MAX_BLOCKS_PER_SPEC = 64
_MAX_ITEMS_PER_LIST = 50
_MAX_DOMAINS_PER_WIDGET = 20
_MAX_PAYLOAD_BYTES = 4 * 1024  # 4KB cap matches the planning doc
_MAX_SPEC_BYTES = 64 * 1024

# Action-registry caps (C1). Bound the declaration surface so a malformed or
# hostile spec can't blow up the tool set / catalog.
_MAX_ACTIONS_PER_SPEC = 16
_MAX_ARGS_PER_ACTION = 12
_MAX_CATALOG_ITEMS = 200
_MAX_CART_ITEMS = 50
# The arg-type names an action may declare — a FLAT map of {name: type-name}.
# Nested/object args are rejected so the tool input schema stays simple and the
# executor's per-arg coercion is total.
_ACTION_ARG_TYPES = frozenset({"str", "int", "float", "bool"})
_ACTION_POLICIES = frozenset({"auto", "gated"})
# SS-2: only these built-in verbs touch VISITOR-scoped state (the visitor's own
# cart / a handoff link) and may therefore carry policy "auto". Every other verb
# MUST be "gated" — a non-cart effect auto-firing would violate the staffed-sites
# rule that tenant-scoped effects only happen through an Instinct proposal.
_AUTO_VERBS = frozenset({"add_to_cart", "checkout"})
# snake_case verb: lowercase, digits, underscores; must start with a letter.
_VERB_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _gen_token() -> str:
    """Per-widget scoped access token — URL-safe, rotatable."""
    return f"pp_tok_{secrets.token_urlsafe(32)}"


# ---------------------------------------------------------------------------
# Render blocks (tagged union via `type`)
# ---------------------------------------------------------------------------


class PawBarAction(BaseModel):
    """An outbound event the widget should post when the block is activated."""

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PawBarListItem(BaseModel):
    title: str
    meta: str = ""
    action: PawBarAction | None = None
    disabled: bool = False


class PawBarFormField(BaseModel):
    name: str
    label: str = ""
    type: Literal["text", "email", "number", "textarea"] = "text"
    placeholder: str = ""
    required: bool = False


class PawBarBlock(BaseModel):
    """Minimal render primitive shared with the widget bundle.

    `type` drives how the bundle renders the block. Every block-specific field
    is optional at the schema level — the renderer only reads fields relevant
    to the active type. Anything else is ignored, so forward-compatible spec
    additions don't break older widget builds.
    """

    type: Literal["text", "image", "list", "button", "form", "divider"]

    # text
    content: str = ""
    style: Literal["body", "heading", "muted"] = "body"

    # image
    src: str = ""
    alt: str = ""

    # list
    items: list[PawBarListItem] = Field(default_factory=list)

    # button
    label: str = ""
    href: str = ""
    action: PawBarAction | None = None

    # form
    fields: list[PawBarFormField] = Field(default_factory=list)
    submit_event: str = ""

    @field_validator("items")
    @classmethod
    def _cap_list(cls, value: list[PawBarListItem]) -> list[PawBarListItem]:
        if len(value) > _MAX_ITEMS_PER_LIST:
            raise ValueError(f"list block accepts at most {_MAX_ITEMS_PER_LIST} items")
        return value


# ---------------------------------------------------------------------------
# Action registry (C1) — the visitor-commerce vocabulary
# ---------------------------------------------------------------------------


class PawBarActionSpec(BaseModel):
    """One declared action the concierge agent may invoke on a visitor's behalf.

    ``policy`` gates the effect (SS-2): ``auto`` verbs touch ONLY visitor-scoped
    state (the visitor's own cart / a handoff link) and fire immediately;
    ``gated`` verbs never execute — they raise an Instinct proposal for a human.
    ``args`` is a FLAT map of ``{arg_name: type-name}`` where the type-name is one
    of ``str|int|float|bool`` — the executor validates and coerces each arg
    against it and rejects unknown keys. ``label`` is the human CTA text the
    widget renders; optional (falls back to the verb).
    """

    verb: str
    policy: Literal["auto", "gated"] = "gated"
    args: dict[str, str] = Field(default_factory=dict)
    label: str = ""

    @field_validator("verb")
    @classmethod
    def _snake_case_verb(cls, value: str) -> str:
        v = value.strip()
        if not _VERB_RE.match(v):
            raise ValueError(
                f"action verb {value!r} must be snake_case "
                "(lowercase letters, digits, underscores; starts with a letter)"
            )
        return v

    @field_validator("args")
    @classmethod
    def _flat_arg_types(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_ARGS_PER_ACTION:
            raise ValueError(f"an action declares at most {_MAX_ARGS_PER_ACTION} args")
        for name, type_name in value.items():
            if not _VERB_RE.match(name):
                raise ValueError(f"action arg name {name!r} must be snake_case")
            if type_name not in _ACTION_ARG_TYPES:
                raise ValueError(
                    f"action arg {name!r} type {type_name!r} must be one of "
                    f"{sorted(_ACTION_ARG_TYPES)} (args are a flat type map)"
                )
        return value


class PawBarCatalogItem(BaseModel):
    """One product the concierge can add to a cart / render on a card."""

    id: str
    name: str
    price_cents: int = 0
    currency: str = "USD"
    image_url: str = ""
    url: str = ""

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("catalog item id is required")
        return value.strip()

    @field_validator("price_cents")
    @classmethod
    def _non_negative_price(cls, value: int) -> int:
        if value < 0:
            raise ValueError("catalog item price_cents must be a non-negative integer")
        return value


class PawBarSpec(BaseModel):
    """The payload the widget fetches and renders."""

    widget_id: str
    pocket_id: str
    layout: Literal["vertical", "horizontal", "grid"] = "vertical"
    theme: dict[str, str] = Field(default_factory=dict)
    blocks: list[PawBarBlock] = Field(default_factory=list)
    # C1 action registry — all optional; a spec without them is unchanged.
    actions: list[PawBarActionSpec] = Field(default_factory=list)
    catalog: list[PawBarCatalogItem] = Field(default_factory=list)
    checkout_url: str = ""

    @field_validator("blocks")
    @classmethod
    def _cap_blocks(cls, value: list[PawBarBlock]) -> list[PawBarBlock]:
        if len(value) > _MAX_BLOCKS_PER_SPEC:
            raise ValueError(f"spec accepts at most {_MAX_BLOCKS_PER_SPEC} blocks")
        return value

    @field_validator("actions")
    @classmethod
    def _cap_and_dedupe_actions(cls, value: list[PawBarActionSpec]) -> list[PawBarActionSpec]:
        if len(value) > _MAX_ACTIONS_PER_SPEC:
            raise ValueError(f"spec accepts at most {_MAX_ACTIONS_PER_SPEC} actions")
        seen: set[str] = set()
        for action in value:
            if action.verb in seen:
                raise ValueError(f"duplicate action verb {action.verb!r} — verbs must be unique")
            seen.add(action.verb)
            # SS-2: a non-cart verb must never be "auto" — only visitor-scoped
            # cart verbs auto-fire; everything else is gated to an Instinct proposal.
            if action.policy == "auto" and action.verb not in _AUTO_VERBS:
                raise ValueError(
                    f"action {action.verb!r} may not use policy 'auto' — only "
                    f"{sorted(_AUTO_VERBS)} touch visitor-scoped state and may auto-fire; "
                    "every other verb must be 'gated'"
                )
        return value

    @field_validator("catalog")
    @classmethod
    def _cap_and_dedupe_catalog(cls, value: list[PawBarCatalogItem]) -> list[PawBarCatalogItem]:
        if len(value) > _MAX_CATALOG_ITEMS:
            raise ValueError(f"spec accepts at most {_MAX_CATALOG_ITEMS} catalog items")
        seen: set[str] = set()
        for item in value:
            if item.id in seen:
                raise ValueError(f"duplicate catalog id {item.id!r} — catalog ids must be unique")
            seen.add(item.id)
        return value

    @field_validator("checkout_url")
    @classmethod
    def _http_checkout_url(cls, value: str) -> str:
        v = value.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("checkout_url must be an http(s) URL")
        return v


# ---------------------------------------------------------------------------
# Widget + Event domain
# ---------------------------------------------------------------------------


class PawBarEventMapping(BaseModel):
    """How an inbound widget event turns into a Fabric object.

    `creates` is the Fabric object type; `fields` values follow `{{ placeholder }}`
    interpolation against the event payload and metadata (`customer_ref`, `timestamp`).
    """

    creates: str
    fields: dict[str, str] = Field(default_factory=dict)


class PawBarWidget(BaseModel):
    id: str = Field(default_factory=lambda: _gen_id("pp"))
    pocket_id: str
    owner: str
    # W4a in-row tenancy — the owning workspace. Empty string means a
    # legacy/single-tenant row (matched by every scoped read, like decisions).
    workspace_id: str = ""
    # T3 concierge binding — the agent that answers this widget's chats. "" =
    # unbound (legacy row / no agent). Mirrors workspace_id above (same default);
    # public read paths stay widget_id-keyed, this is just carried through.
    agent_id: str = ""
    name: str = ""
    spec: PawBarSpec
    allowed_domains: list[str] = Field(default_factory=list)
    access_token: str = Field(default_factory=_gen_token)
    rate_limit_per_min: int = 60
    per_customer_limit_per_min: int = 10
    event_mapping: dict[str, PawBarEventMapping] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @field_validator("allowed_domains")
    @classmethod
    def _cap_domains(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_DOMAINS_PER_WIDGET:
            raise ValueError(f"allowed_domains accepts at most {_MAX_DOMAINS_PER_WIDGET} entries")
        cleaned: list[str] = []
        for domain in value:
            d = domain.strip().lower()
            if d and d not in cleaned:
                cleaned.append(d)
        return cleaned

    @field_validator("rate_limit_per_min", "per_customer_limit_per_min")
    @classmethod
    def _positive_rate(cls, value: int) -> int:
        if value < 1:
            raise ValueError("rate limits must be >= 1")
        return value


class PawBarWidgetPublic(BaseModel):
    """Token-free projection of :class:`PawBarWidget`.

    Used as the response model for list/read endpoints. It carries every
    widget field EXCEPT ``access_token`` — the per-widget owner credential
    that authorizes mutating + event-read operations. That secret must never
    leave the server in a list/read payload; it is returned only by the
    explicit, authenticated create and rotate-token paths.

    Build one with :meth:`from_widget` so the projection stays in lockstep
    with the source model.
    """

    id: str
    pocket_id: str
    owner: str
    workspace_id: str = ""
    # T3 — mirror of PawBarWidget.agent_id; the token-free projection carries it
    # too (it is not a secret, just the binding).
    agent_id: str = ""
    name: str = ""
    spec: PawBarSpec
    allowed_domains: list[str] = Field(default_factory=list)
    rate_limit_per_min: int
    per_customer_limit_per_min: int
    event_mapping: dict[str, PawBarEventMapping] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_widget(cls, widget: PawBarWidget) -> PawBarWidgetPublic:
        data = widget.model_dump()
        data.pop("access_token", None)
        return cls(**data)


class PawBarEvent(BaseModel):
    """One inbound signal from a rendered widget."""

    widget_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    customer_ref: str
    timestamp: datetime = Field(default_factory=datetime.now)

    def payload_size(self) -> int:
        import json as _json

        try:
            return len(_json.dumps(self.payload).encode("utf-8"))
        except Exception:
            return _MAX_PAYLOAD_BYTES + 1

    @field_validator("type")
    @classmethod
    def _non_empty_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event type is required")
        return value.strip()


# ---------------------------------------------------------------------------
# Decision delivery — the back-half of the customer decision loop (gap2)
# ---------------------------------------------------------------------------


class DecisionState(StrEnum):
    """Where a customer's request sits in the decision loop.

    ``PENDING``   — the event raised an Instinct proposal; a human has not yet
                    decided. The customer surface shows "we're looking into it".
    ``DELIVERED`` — the human approved; ``reply`` carries the answer the
                    customer can read.
    ``DECLINED``  — the human rejected; ``reply`` carries the (optional) reason.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    DECLINED = "declined"


class DecisionStatus(BaseModel):
    """A decision made (or pending) for one inbound customer event.

    This is the deliverable the customer surface polls back: the widget posted
    an event, a human decided, and the answer lands here keyed by
    ``(widget_id, customer_ref)`` so the rendered widget can retrieve it with no
    owner credential. ``instinct_action_id`` ties the row to the Instinct
    proposal that drove the decision, so the audit trail is reconstructable.
    ``workspace_id`` scopes the row to the owning tenant.
    """

    id: str = Field(default_factory=lambda: _gen_id("ppd"))
    widget_id: str
    customer_ref: str
    event_type: str = ""
    instinct_action_id: str = ""
    workspace_id: str = ""
    state: DecisionState = DecisionState.PENDING
    reply: str = ""
    decided_by: str = ""
    # PII INVARIANT (binding): the visitor's optional contact email lives ONLY
    # on this DecisionStatus row. It must never be copied into the Instinct
    # Action / its ``_customer_reply`` blob, the agent's context, the KB,
    # transcripts, or the soul — and it is never echoed back by any public
    # read (the decision poll response omits it). Storage is capped here at
    # the row level; the only consumer is the one-shot email the delivery
    # hook sends when the row flips out of PENDING.
    contact_email: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Visitor cart (C1) — the visitor-scoped state the store persists per
# (widget_id, customer_ref). "auto" add_to_cart upserts here; no TTL in v1.
# ---------------------------------------------------------------------------


class PawBarCartItem(BaseModel):
    """One line in a visitor's cart — a catalog snapshot plus a quantity."""

    id: str
    name: str
    price_cents: int = 0
    currency: str = "USD"
    qty: int = 1


class PawBarCart(BaseModel):
    """A visitor's cart summary — what GET /paw-bar/cart returns.

    Keyed by ``(widget_id, customer_ref)`` in the store; this value object is the
    read model the endpoint + the executor return. ``total_cents`` is derived
    from the items so callers never re-sum.
    """

    widget_id: str
    customer_ref: str
    items: list[PawBarCartItem] = Field(default_factory=list)
    currency: str = "USD"
    checkout_url: str = ""
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def total_cents(self) -> int:
        return sum(item.price_cents * item.qty for item in self.items)


# ---------------------------------------------------------------------------
# Limit constants — re-exported so the ingest layer (PR-B) reads the same values.
# ---------------------------------------------------------------------------

MAX_BLOCKS_PER_SPEC = _MAX_BLOCKS_PER_SPEC
MAX_ITEMS_PER_LIST = _MAX_ITEMS_PER_LIST
MAX_DOMAINS_PER_WIDGET = _MAX_DOMAINS_PER_WIDGET
MAX_PAYLOAD_BYTES = _MAX_PAYLOAD_BYTES
MAX_SPEC_BYTES = _MAX_SPEC_BYTES
MAX_ACTIONS_PER_SPEC = _MAX_ACTIONS_PER_SPEC
MAX_ARGS_PER_ACTION = _MAX_ARGS_PER_ACTION
MAX_CATALOG_ITEMS = _MAX_CATALOG_ITEMS
MAX_CART_ITEMS = _MAX_CART_ITEMS
ACTION_ARG_TYPES = _ACTION_ARG_TYPES
ACTION_POLICIES = _ACTION_POLICIES
