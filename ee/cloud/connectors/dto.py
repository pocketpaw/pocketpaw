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


# ---------------------------------------------------------------------------
# Phase 1 PR-2 — widget recipes + action execution
# ---------------------------------------------------------------------------


class WidgetRecipeResponse(BaseModel):
    """One widget recipe contributed by an enabled connector.

    Returned by ``GET /api/v1/cloud/connectors/widget-recipes`` to feed
    ``AddWidgetPicker`` 's "From connectors" rail. The frontend compiles
    each recipe into a Ripple UISpec at render time, so the wire shape
    here stays Ripple-version-agnostic.
    """

    connector: str  # e.g. "gmail"
    connector_display_name: str  # "Gmail"
    title: str
    display_type: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    default_size: str = "col-1 row-1"
    description: str = ""


class ExecuteActionRequest(BaseModel):
    """POST /connectors/{name}/execute body.

    ``scope`` selects which scope's credentials are used at execute
    time. When the connector's action is ``execution_mode=local``, the
    cloud router forwards the call to the user's pocketpaw runtime via
    the chat WebSocket bus (see CHARTER.md §6.2).
    """

    action: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="workspace", pattern="^(pocket|workspace|user)$")
    pocket_id: str | None = None
    user_id: str | None = None


class ExecuteActionResponse(BaseModel):
    """Result envelope for /connectors/{name}/execute.

    ``execution_mode`` echoes back where the action ran so the frontend
    can show a "ran on your machine" badge for local-mode actions.
    """

    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0
    execution_mode: str = "cloud"
