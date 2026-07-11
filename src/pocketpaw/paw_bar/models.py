# ee/paw_bar/models.py — Pydantic models for the Paw Bar widget layer.
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


class PawBarSpec(BaseModel):
    """The payload the widget fetches and renders."""

    widget_id: str
    pocket_id: str
    layout: Literal["vertical", "horizontal", "grid"] = "vertical"
    theme: dict[str, str] = Field(default_factory=dict)
    blocks: list[PawBarBlock] = Field(default_factory=list)

    @field_validator("blocks")
    @classmethod
    def _cap_blocks(cls, value: list[PawBarBlock]) -> list[PawBarBlock]:
        if len(value) > _MAX_BLOCKS_PER_SPEC:
            raise ValueError(f"spec accepts at most {_MAX_BLOCKS_PER_SPEC} blocks")
        return value


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
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Limit constants — re-exported so the ingest layer (PR-B) reads the same values.
# ---------------------------------------------------------------------------

MAX_BLOCKS_PER_SPEC = _MAX_BLOCKS_PER_SPEC
MAX_ITEMS_PER_LIST = _MAX_ITEMS_PER_LIST
MAX_DOMAINS_PER_WIDGET = _MAX_DOMAINS_PER_WIDGET
MAX_PAYLOAD_BYTES = _MAX_PAYLOAD_BYTES
MAX_SPEC_BYTES = _MAX_SPEC_BYTES
