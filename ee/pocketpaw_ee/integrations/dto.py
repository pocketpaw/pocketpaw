# ee/pocketpaw_ee/integrations/dto.py: the wire shapes of the integrations API.
#
# Pinned by the Wave 0 wire contract (docs/design/drafts/2026-10-13-sites-integrations.md
# in paw-workspace): paw-enterprise and paw-sites' paw.js build against these field
# names and enum values, so rename nothing. No view carries a token.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SiteIntegrationStatus = Literal["connected", "needs_connection", "needs_reconnect", "disabled"]
ConnectionStatus = Literal["active", "needs_reconnect", "revoked"]
OrderStatus = Literal["pending", "paid", "refunded", "failed"]


class ActionView(BaseModel):
    name: str
    public: bool
    write: bool
    description: str


class ProviderView(BaseModel):
    id: str
    name: str
    kind: Literal["builtin", "connect", "embed"]
    category: str
    plan: Literal["free", "site"]
    availability: Literal["available", "coming_soon", "unconfigured"]
    actions: list[ActionView] = Field(default_factory=list)


class ConnectionView(BaseModel):
    id: str
    provider: str
    account_label: str
    status: ConnectionStatus
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    site_count: int = 0


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
