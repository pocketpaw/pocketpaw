# Senses — response schemas for the browsable connector catalog.
# Created: 2026-07-16 (SR-2 catalog listing API) — the wire shape behind
#   ``GET /api/v1/cloud/senses/catalog``: the whole connector catalog grouped by
#   category, each connector carrying its actions (trust + execution mode +
#   availability + a ``cost_estimate`` placeholder), its declared senses, and a
#   per-tenant ``bound`` flag. Read-only browse surface (the data behind a
#   tools-style front door), so there is no request schema — the endpoint takes
#   only an optional ``pocket_id`` query param. These mirror the pure
#   ``cloud.senses.catalog`` dataclasses (``CatalogCategoryGroup`` /
#   ``CatalogConnectorEntry`` / ``CatalogActionEntry``); the service maps
#   domain -> DTO so the catalog module stays Pydantic-free + unit-testable.

from __future__ import annotations

from pydantic import BaseModel, Field


class CatalogActionResponse(BaseModel):
    """One connector action in the browse catalog.

    ``available`` is False for ``local`` / ``sandbox`` execution modes the shared
    cloud cannot dispatch (same rule as ``sense_search``); ``unavailable_reason``
    names why. ``cost_estimate`` is a placeholder (None) until per-action pricing
    ships in a later task.
    """

    action: str
    description: str
    trust_level: str  # "auto" | "confirm" | "restricted"
    execution_mode: str  # "cloud" | "local" | "sandbox"
    available: bool
    unavailable_reason: str | None = None
    cost_estimate: float | None = None


class CatalogConnectorResponse(BaseModel):
    """One connector in the browse catalog, with its actions + per-tenant state.

    ``bound`` is True when the connector is enabled + reachable from the resolved
    pocket (workspace-scope when no ``pocket_id`` is given). ``senses`` are the
    provider-agnostic capabilities the connector declares.
    """

    connector: str
    display_name: str
    type: str  # category — the connector def's ``type`` field
    senses: list[str] = Field(default_factory=list)
    bound: bool = False
    actions: list[CatalogActionResponse] = Field(default_factory=list)


class CatalogCategoryResponse(BaseModel):
    """Every connector sharing one category (the connector def's ``type``)."""

    category: str
    connectors: list[CatalogConnectorResponse] = Field(default_factory=list)
