# ee/pocketpaw_ee/integrations/dto.py: the wire shapes of the integrations API.
#
# Pinned by the Wave 0 wire contract and its Wave 1 addendum
# (docs/design/drafts/2026-10-13-sites-integrations.md in paw-workspace):
# paw-enterprise and paw-sites' paw.js build against these field names and enum
# values, so rename nothing; changes are additive. No view carries a token.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SiteIntegrationStatus = Literal[
    "connected", "needs_connection", "needs_reconnect", "setup_required", "disabled"
]
ConnectionStatus = Literal["active", "needs_reconnect", "revoked"]
OrderStatus = Literal["pending", "paid", "refunded", "failed"]


class ActionView(BaseModel):
    name: str
    public: bool
    write: bool
    description: str


class SettingOptionView(BaseModel):
    value: str
    label: str


class SettingsFieldView(BaseModel):
    key: str
    label: str
    # "link" + readonly = a server-set address the client shows as an open link.
    type: Literal["url", "text", "select", "link"]
    help: str | None = None
    required: bool = False
    options: list[SettingOptionView] | None = None
    readonly: bool = False
    # Client mirrors of the server checks: comma-separated multi select, url hosts, length cap.
    multi: bool = False
    allowed_hosts: list[str] | None = None
    max_length: int | None = None


class ProviderView(BaseModel):
    id: str
    name: str
    kind: Literal["builtin", "connect", "embed"]
    category: str
    plan: Literal["free", "site"]
    availability: Literal["available", "coming_soon", "unconfigured"]
    actions: list[ActionView] = Field(default_factory=list)
    settings_fields: list[SettingsFieldView] = Field(default_factory=list)
    # False for builtin and embed kinds: nothing to connect, just link and configure.
    requires_connection: bool = False


class ConnectionView(BaseModel):
    id: str
    provider: str
    account_label: str
    status: ConnectionStatus
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    site_count: int = 0
    # Owner-facing, e.g. "Finish setting up your Stripe account"; null when fine.
    attention: str | None = None


class SiteIntegrationView(BaseModel):
    provider: str
    connection_id: str | None = None
    status: SiteIntegrationStatus
    settings: dict[str, Any] = Field(default_factory=dict)
    enabled_actions: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class OrderItemView(BaseModel):
    ref: str
    name: str
    qty: int
    amount_minor: int


class OrderView(BaseModel):
    id: str
    provider: str
    external_id: str
    status: OrderStatus
    amount_minor: int
    currency: str
    customer_email: str | None = None
    items: list[OrderItemView] = Field(default_factory=list)
    created_at: datetime | None = None


class OrdersPage(BaseModel):
    items: list[OrderView] = Field(default_factory=list)
    next_cursor: str | None = None


class ConnectBody(BaseModel):
    pocket_id: str | None = None


class ConnectResponse(BaseModel):
    authorize_url: str


class SiteIntegrationPut(BaseModel):
    connection_id: str | None = None
    settings: dict[str, Any] | None = None
    enabled_actions: list[str] | None = None
