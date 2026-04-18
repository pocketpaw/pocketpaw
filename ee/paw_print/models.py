# ee/paw_print/models.py — Pydantic models for the Paw Print widget layer.
# Created: 2026-04-13 (Move 3 PR-A) — Minimal, secure-by-design render vocabulary
# (text / image / list / button / form / divider). No raw HTML, no script
# injection paths. The widget.js bundle consumes PawPrintSpec; the backend
# consumes PawPrintEvent on the ingest side.

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ee.fabric.models import _gen_id

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


class PawPrintAction(BaseModel):
    """An outbound event the widget should post when the block is activated."""

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PawPrintListItem(BaseModel):
    title: str
    meta: str = ""
    action: PawPrintAction | None = None
    disabled: bool = False


class PawPrintFormField(BaseModel):
    name: str
    label: str = ""
    type: Literal["text", "email", "number", "textarea"] = "text"
    placeholder: str = ""
    required: bool = False


class PawPrintBlock(BaseModel):
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
    items: list[PawPrintListItem] = Field(default_factory=list)

    # button
    label: str = ""
    href: str = ""
    action: PawPrintAction | None = None

    # form
    fields: list[PawPrintFormField] = Field(default_factory=list)
    submit_event: str = ""

    @field_validator("items")
    @classmethod
    def _cap_list(cls, value: list[PawPrintListItem]) -> list[PawPrintListItem]:
        if len(value) > _MAX_ITEMS_PER_LIST:
            raise ValueError(f"list block accepts at most {_MAX_ITEMS_PER_LIST} items")
        return value


class PawPrintSpec(BaseModel):
    """The payload the widget fetches and renders."""

    widget_id: str
    pocket_id: str
    layout: Literal["vertical", "horizontal", "grid"] = "vertical"
    theme: dict[str, str] = Field(default_factory=dict)
    blocks: list[PawPrintBlock] = Field(default_factory=list)

    @field_validator("blocks")
    @classmethod
    def _cap_blocks(cls, value: list[PawPrintBlock]) -> list[PawPrintBlock]:
        if len(value) > _MAX_BLOCKS_PER_SPEC:
            raise ValueError(f"spec accepts at most {_MAX_BLOCKS_PER_SPEC} blocks")
        return value


# ---------------------------------------------------------------------------
# Widget + Event domain
# ---------------------------------------------------------------------------


class PawPrintEventMapping(BaseModel):
    """How an inbound widget event turns into a Fabric object.

    `creates` is the Fabric object type; `fields` values follow `{{ placeholder }}`
    interpolation against the event payload and metadata (`customer_ref`, `timestamp`).
    """

    creates: str
    fields: dict[str, str] = Field(default_factory=dict)


class PawPrintWidget(BaseModel):
    id: str = Field(default_factory=lambda: _gen_id("pp"))
    pocket_id: str
    owner: str
    name: str = ""
    spec: PawPrintSpec
    allowed_domains: list[str] = Field(default_factory=list)
    access_token: str = Field(default_factory=_gen_token)
    rate_limit_per_min: int = 60
    per_customer_limit_per_min: int = 10
    event_mapping: dict[str, PawPrintEventMapping] = Field(default_factory=dict)
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


class PawPrintEvent(BaseModel):
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
# Limit constants — re-exported so the ingest layer (PR-B) reads the same values.
# ---------------------------------------------------------------------------

MAX_BLOCKS_PER_SPEC = _MAX_BLOCKS_PER_SPEC
MAX_ITEMS_PER_LIST = _MAX_ITEMS_PER_LIST
MAX_DOMAINS_PER_WIDGET = _MAX_DOMAINS_PER_WIDGET
MAX_PAYLOAD_BYTES = _MAX_PAYLOAD_BYTES
MAX_SPEC_BYTES = _MAX_SPEC_BYTES
