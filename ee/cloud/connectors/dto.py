# Connectors — request / response schemas.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Every request schema is distinct from every response schema (cloud
# rule §4). The wire shape mirrors the existing
# src/pocketpaw/api/v1/connectors.py ``ConnectorInfo`` so the frontend
# type stays unchanged when the cloud handler shadows the runtime one.

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class EnableConnectorRequest(BaseModel):
    """POST /connectors/{name}/enable body.

    ``scope`` is one of ``pocket | workspace | user``. ``pocket_id`` and
    ``user_id`` are required when scope picks them; the service raises
    ``ValidationError`` if they're missing or set on the wrong scope.
    """

    scope: str = Field(default="workspace", pattern="^(pocket|workspace|user)$")
    pocket_id: str | None = None
    user_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateConnectorConfigRequest(BaseModel):
    """PATCH /connectors/{name}/config body."""

    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Responses — match existing ConnectorInfo wire shape for frontend compat
# ---------------------------------------------------------------------------


class ConnectorResponse(BaseModel):
    """Single row in the GET /connectors list.

    Mirrors ``src/pocketpaw/api/v1/connectors.py:ConnectorInfo`` exactly
    so paw-enterprise's ``getConnectors()`` keeps working unchanged. The
    extended fields below are optional additions for the cloud-only
    consumer.
    """

    name: str
    display_name: str
    type: str
    icon: str
    status: str = "disconnected"  # "connected" | "disconnected" | "error"

    # Cloud-only extensions — frontend already tolerates extra fields.
    enabled: bool = False
    scope: str | None = None
    last_sync_at: datetime | None = None
    last_sync_status: str = "never"


class ConnectorDetailResponse(ConnectorResponse):
    """Single connector detail returned by GET /connectors/{name}."""

    actions: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
