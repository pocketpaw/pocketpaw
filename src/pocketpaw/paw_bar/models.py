# src/pocketpaw/paw_bar/models.py — Pydantic models for the Paw Bar widget layer.
#
# The render vocabulary the widget bundle draws (``PawBarBlock``: text / image /
# list / button / form / divider, no raw HTML, no script paths) inside a
# ``PawBarSpec``, plus the visitor-commerce declarations on that spec: ``actions``
# (unique snake_case verbs; only the cart verbs may be ``auto``, everything else is
# ``gated`` to an Instinct proposal; the built-in ``send_to_team`` is reserved and
# can't be declared) and an http(s) ``checkout_url``. The product
# catalog lives in its own table (``paw_bar.catalog_store``, rows read back as
# ``PawBarCatalogRow``); ``PawBarSpec.catalog`` is DEPRECATED, kept one release so
# an older editor's spec PATCH still works: the store adds a non-empty one to the
# table on write (upsert by id, never a delete) and never stores it in the spec.
# Its 200-item validator stays, since only those older clients send it.
# ``spec_bytes`` is what the 64 KB spec cap (``MAX_SPEC_BYTES``) measures: the
# spec without its catalog. Catalog items are the ONLY source of product data on a
# card, so they are cleaned as untrusted input whether typed by the owner or
# imported from the store's site: text truncated to its cap, a non-http(s) image
# url or a ``url`` that is neither http(s) nor a single-slash site path blanked,
# a bad currency read as USD, a price past ``MAX_CATALOG_PRICE_MINOR`` read as 0.
# Cleaned, not rejected, because a stored spec is re-validated on every load
# and must never become unloadable. Every amount
# (``price_cents`` …) is ISO 4217 minor units of its currency (see
# ``pocketpaw.money``); a cart holds one currency only.
#
# Also here: the widget row (``PawBarWidget``; ``PawBarWidgetPublic`` is its
# token-free projection for reads, so the per-widget access token only leaves the
# server on create / rotate), ingest events and their Fabric mappings, the
# visitor cart, ``DecisionStatus`` (the owner's decision a visitor polls back,
# keyed by widget + customer_ref; an optional contact email lives only on that
# row), and the owner-inbox rows: ``Conversation`` (lifecycle over run docs, no
# messages) and ``OwnerMessage`` (owner / system / muted-visitor lines that are
# deliberately NOT run docs, so metering never bills them).

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from pocketpaw.fabric.models import _gen_id
from pocketpaw.money import normalize_currency

logger = logging.getLogger(__name__)

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
_MAX_CATALOG_NAME_CHARS = 200
_MAX_CATALOG_DESCRIPTION_CHARS = 300
_MAX_CATALOG_URL_CHARS = 2048
MAX_CATALOG_PRICE_MINOR = 10**12  # past this a price is a typo or a parse accident
# Currency values already warned about, so a bad stored spec logs once, not per load.
_WARNED_CURRENCIES: set[str] = set()
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
# Built-in verbs the executor handles itself (``send_to_team``: the concierge's
# lead card). An owner can't declare one, so the name can only ever mean that.
_RESERVED_VERBS = frozenset({"send_to_team"})
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
    """One product the concierge can add to a cart / render on a card.

    ``price_cents`` is ISO 4217 minor units of ``currency`` (¥1,500 is 1500,
    $3.50 is 350, 1.250 KWD is 1250); the name is historical. ``url`` is the
    product's page: an absolute http(s) URL or a site path (``/products/mug``),
    which is how imported items store it. ``in_stock`` is None when the stock is
    unknown; False marks the item sold out in the prompt.
    """

    id: str
    name: str
    price_cents: int = 0
    currency: str = "USD"
    image_url: str = ""
    url: str = ""
    description: str = ""
    in_stock: bool | None = None

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("catalog item id is required")
        return value.strip()

    # The text and link fields SANITISE rather than reject: specs are re-validated
    # from SQLite on every load, so a strict rule here would make a widget saved
    # before the rule existed unloadable. Only the id and price stay hard rules.

    @field_validator("name", mode="before")
    @classmethod
    def _cap_name(cls, value: Any) -> Any:
        return value.strip()[:_MAX_CATALOG_NAME_CHARS] if isinstance(value, str) else value

    @field_validator("description", mode="before")
    @classmethod
    def _cap_description(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()[:_MAX_CATALOG_DESCRIPTION_CHARS]
        return value

    @field_validator("price_cents")
    @classmethod
    def _non_negative_price(cls, value: int) -> int:
        if value < 0:
            raise ValueError("catalog item price_cents must be a non-negative integer")
        if value > MAX_CATALOG_PRICE_MINOR:
            # Coerced like the text fields: a stored spec must stay loadable.
            logger.warning(
                "paw_bar: catalog price_cents %d is past %d; reading it as 0",
                value,
                MAX_CATALOG_PRICE_MINOR,
            )
            return 0
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_code(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return normalize_currency(value)
        except ValueError:
            pass
        if value not in _WARNED_CURRENCIES and len(_WARNED_CURRENCIES) < 256:
            _WARNED_CURRENCIES.add(value)
            logger.warning("paw_bar: catalog currency %r is not a 3-letter code; using USD", value)
        return "USD"

    @field_validator("image_url", mode="before")
    @classmethod
    def _http_image_url(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        v = value.strip()
        if len(v) > _MAX_CATALOG_URL_CHARS or not v.lower().startswith(("http://", "https://")):
            return ""
        return v

    @field_validator("url", mode="before")
    @classmethod
    def _http_or_path_url(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        v = value.strip()
        is_path = v.startswith("/") and not v.startswith("//")
        is_http = v.lower().startswith(("http://", "https://"))
        if len(v) > _MAX_CATALOG_URL_CHARS or not (is_path or is_http):
            return ""
        return v


class PawBarCatalogRow(PawBarCatalogItem):
    """A catalog item as the catalog store holds it: the item plus its place in
    the owner's order, where it came from (``manual`` | ``shopify`` |
    ``woocommerce`` | ``jsonld`` | ``opengraph`` | ``csv`` | ``site``), when it
    was last written, and who owns it: ``origin`` is ``site`` while the site
    sync keeps it in step with the site, ``owner`` once the owner made or edited
    it (the sync never touches those)."""

    position: int = 0
    source: str = "manual"
    updated_at: str = ""
    origin: Literal["site", "owner"] = "owner"


class PawBarSpec(BaseModel):
    """The payload the widget fetches and renders.

    ``catalog`` is deprecated: the catalog is stored in ``paw_bar_catalog_items``.
    A stored spec carries an empty one; the frozen public spec endpoint fills it
    from the store on the way out."""

    widget_id: str
    pocket_id: str
    layout: Literal["vertical", "horizontal", "grid"] = "vertical"
    theme: dict[str, str] = Field(default_factory=dict)
    blocks: list[PawBarBlock] = Field(default_factory=list)
    # C1 action registry — all optional; a spec without them is unchanged.
    actions: list[PawBarActionSpec] = Field(default_factory=list)
    # Deprecated (see the class docstring); only an older client's PATCH sets it.
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
            if action.verb in _RESERVED_VERBS:
                raise ValueError(f"action verb {action.verb!r} is built in and can't be declared")
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
# Conversation lifecycle — the owner inbox's state row (slice 1)
#
# A concierge conversation already EXISTS as a stream of ChatRunDoc rows keyed by
# (workspace, context_type='concierge', scope_id, customer_ref). What it lacks is
# a place to say "this one still needs me". These models are that place, and
# nothing more: no messages, no copies of the transcript. One row per
# (widget_id, customer_ref), created lazily on the visitor's first turn, so the
# log becomes a queue without a backfill.
# ---------------------------------------------------------------------------


def _gen_conversation_id() -> str:
    """Fresh conversation row id (``ppc_…``).

    Named rather than inlined so the model default and the store's INSERT mint
    ids the same way — the store writes rows with raw SQL, so without this the
    prefix would live in two places and drift.
    """
    return _gen_id("ppc")


class ConversationState(StrEnum):
    """Where a visitor conversation sits in the owner's queue.

    ``OPEN``        — live; the bot is handling it (or the owner is) and it sits
                      in the default inbox view.
    ``NEEDS_HUMAN`` — escalated: the visitor asked for a person, or the bot could
                      not answer. It is the ONE state a visitor reply does not
                      change — already at the top of the queue, nowhere to raise.
    ``SNOOZED``     — deliberately hidden until ``snooze_until``. Expiry is
                      computed on READ (see the store) — there is no sweeper, so
                      a snooze always ends on time even if nothing is running.
    ``CLOSED``      — done. A new visitor message re-opens it automatically.
    """

    OPEN = "open"
    NEEDS_HUMAN = "needs_human"
    SNOOZED = "snoozed"
    CLOSED = "closed"


class ConversationNote(BaseModel):
    """One private operator note on a conversation — never shown to the visitor.

    Notes are append-only from the owner API (a PATCH carrying ``note`` appends
    rather than replaces), so the internal thread of "what we know about this
    person" survives every other edit. ``at`` is an ISO timestamp string, matching
    how the row's other time fields are stored.
    """

    author: str = ""
    text: str = ""
    at: str = ""

    @field_validator("author", mode="before")
    @classmethod
    def _author_to_str(cls, value: Any) -> str:
        """Same ObjectId-vs-str coercion as :class:`OwnerMessage.author`.

        The router already stringifies before calling, but the model is the one
        place every caller passes through — belt and braces against the seam
        that has now bitten this layer four separate times.
        """
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)


class Conversation(BaseModel):
    """The lifecycle + operator metadata for ONE visitor conversation.

    Identity is ``(widget_id, customer_ref)`` — 1:1 with the concierge run
    stream's ``session_key`` — and the row is deliberately thin: the transcript is
    NOT here, it stays derived from the run docs. What lives here is only what the
    runs cannot express: the queue state, whether the bot is muted, the owner's
    tags and private notes, and the unread counter.

    ``workspace_id`` is the REAL tenant workspace (the concierge run's
    ``ctx.workspace_id``), unlike ``DecisionStatus.workspace_id``, which stores
    the widget owner — so scoped reads here are a true tenancy filter.

    ``snooze_until`` / ``last_visitor_at`` / ``last_owner_at`` are ISO strings
    rather than datetimes so the store can compare them in SQL (the snooze-expiry
    CASE) without a round trip through Python.

    PII posture: ``contact_email`` mirrors the invariant on
    ``DecisionStatus.contact_email`` — a visitor-supplied address, owner-visible
    only. It must never reach the Instinct action, the agent's context, the KB,
    the transcript, the soul, or any PUBLIC read. Slice 1 never writes it (the
    owner list derives the display name from the decision row that captured it);
    the column exists so a later slice can promote it explicitly.
    """

    id: str = Field(default_factory=_gen_conversation_id)
    widget_id: str
    customer_ref: str
    workspace_id: str = ""
    state: ConversationState = ConversationState.OPEN
    bot_paused: bool = False
    snooze_until: str = ""
    # Present for the operator toolkit later; there is no assignment UI in v1
    # (solo-owner posture), so nothing writes it yet.
    assignee: str = ""
    tags: list[str] = Field(default_factory=list)
    notes: list[ConversationNote] = Field(default_factory=list)
    contact_email: str = ""
    last_visitor_at: str = ""
    last_owner_at: str = ""
    # When the bot was muted (slice 2). Set whenever ``bot_paused`` flips on,
    # cleared when it flips off. The idle auto-resume reads it, and the owner UI
    # uses it to say when the bot hands itself back.
    bot_paused_at: str = ""
    unread_for_owner: int = 0
    # 2026-08-19 (conversation identity): is this the visitor's conversation IN
    # PROGRESS? A visitor may own several — starting over retires the current one
    # (``active = False``) and opens a fresh one — and a partial unique index
    # keeps at most one active per (widget, visitor), so a chat turn that names no
    # conversation still resolves to exactly one row. Defaults True because every
    # row written before this existed was that visitor's only conversation.
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


def _gen_owner_message_id() -> str:
    """Fresh owner-message row id (``ppm_…``) — same reason as the conversation id."""
    return _gen_id("ppm")


class OwnerMessageRole(StrEnum):
    """Who spoke a line that has no run doc behind it.

    ``OWNER``   — a human on the site's team typed it. The line the visitor is
                  waiting for; the whole point of the takeover.
    ``SYSTEM``  — the product explaining itself in the thread ("the assistant is
                  answering again"). Visitor-facing, but authored by nobody.
    ``VISITOR`` — a visitor message that arrived while the bot was MUTED. It never
                  became a ``ChatRunDoc`` because no run was dispatched (that is
                  the entire point of muting), so without this row the owner's
                  transcript would simply stop mid-conversation at the moment a
                  human took over. Never returned by the public poll: the visitor
                  already has their own words on screen, and echoing them back is
                  how a public read starts leaking a thread.
    """

    OWNER = "owner"
    SYSTEM = "system"
    VISITOR = "visitor"


class OwnerMessage(BaseModel):
    """One thread line stored outside the run stream (owner inbox, slice 2).

    The transcript is normally derived from ``ChatRunDoc`` — one run per visitor
    turn, carrying both halves. These are the lines with no run: see
    :class:`OwnerMessageRole`. Keyed by ``(widget_id, customer_ref)``, the same
    identity as :class:`Conversation`, so the reader merges the two sources on one
    key and one clock.

    ``created_at`` is an ISO-8601 string in UTC (timezone-aware), deliberately NOT
    a naive local stamp like the conversation row's: this value is sorted against
    ``ChatRunDoc.createdAt`` (aware UTC) to interleave the thread, and it is
    handed to clients. A naive local stamp would interleave wrongly by the host's
    UTC offset on every machine that isn't set to UTC.

    PII posture: ``content`` is free text an owner or a visitor typed, so treat it
    as personal data — same class as ``ChatRunDoc.user_text``. ``author`` is the
    owner's user id, and is owner-facing only: the public poll projects role +
    content + timestamp and nothing else.
    """

    id: str = Field(default_factory=_gen_owner_message_id)
    widget_id: str
    customer_ref: str
    #: The conversation this line was said in (2026-08-19). Empty only for lines
    #: written before the column existed and never backfilled — see the store's
    #: migration. It is what stops an owner's reply appearing in a thread it does
    #: not belong to.
    conversation_id: str = ""
    workspace_id: str = ""
    role: OwnerMessageRole = OwnerMessageRole.OWNER
    content: str = ""
    author: str = ""
    created_at: str = ""

    @field_validator("author", mode="before")
    @classmethod
    def _author_to_str(cls, value: Any) -> str:
        """Coerce the caller id, which arrives as a ``PydanticObjectId``.

        The authenticated principal (``current_active_user``) carries an
        ObjectId, not a str, so an owner reply built straight from it failed
        string_type validation and the endpoint 500'd. Every unit test hands in
        a plain string, which is why the suites were green and only a live
        reply surfaced it — the fourth time this exact ObjectId-vs-str seam bit
        this layer, so it is coerced at the MODEL now rather than at each of
        the call sites that keep forgetting.
        """
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# Visitor cart (C1) — the visitor-scoped state the store persists per
# (widget_id, customer_ref). "auto" add_to_cart upserts here; no TTL in v1.
# ---------------------------------------------------------------------------


class PawBarCartItem(BaseModel):
    """One line in a visitor's cart — a catalog snapshot plus a quantity.

    ``price_cents`` is ISO 4217 minor units of ``currency``; the name is historical.
    """

    id: str
    name: str
    price_cents: int = 0
    currency: str = "USD"
    qty: int = 1

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_code(cls, value: Any) -> Any:
        return _cart_currency(value)


def _cart_currency(value: Any) -> Any:
    """Upper-case a stored cart currency; a malformed one reads as USD (rows
    written before normalisation must stay loadable)."""
    if not isinstance(value, str):
        return value
    try:
        return normalize_currency(value)
    except ValueError:
        return "USD"


class PawBarCart(BaseModel):
    """A visitor's cart summary — what GET /paw-bar/cart returns.

    Keyed by ``(widget_id, customer_ref)`` in the store; this value object is the
    read model the endpoint + the executor return. ``total_cents`` is derived
    from the items so callers never re-sum. A cart holds ONE currency: the store
    refuses a line in another currency (``cart_currency_mismatch``), so the total
    is always ISO 4217 minor units of ``currency`` (the name is historical).
    """

    widget_id: str
    customer_ref: str
    items: list[PawBarCartItem] = Field(default_factory=list)
    currency: str = "USD"
    checkout_url: str = ""
    updated_at: datetime = Field(default_factory=datetime.now)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_code(cls, value: Any) -> Any:
        return _cart_currency(value)

    @property
    def total_cents(self) -> int:
        return sum(item.price_cents * item.qty for item in self.items)


def spec_bytes(spec: PawBarSpec) -> int:
    """The size the spec cap measures: the serialized spec WITHOUT its catalog,
    which lives in its own table and has its own item cap."""
    return len(spec.model_copy(update={"catalog": []}).model_dump_json().encode("utf-8"))


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
MAX_CATALOG_NAME_CHARS = _MAX_CATALOG_NAME_CHARS
MAX_CATALOG_DESCRIPTION_CHARS = _MAX_CATALOG_DESCRIPTION_CHARS
MAX_CATALOG_URL_CHARS = _MAX_CATALOG_URL_CHARS
MAX_CART_ITEMS = _MAX_CART_ITEMS
ACTION_ARG_TYPES = _ACTION_ARG_TYPES
ACTION_POLICIES = _ACTION_POLICIES
