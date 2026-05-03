# Connectors — FastAPI router.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Mounted at /api/v1/connectors via mount_cloud(). Wire shape mirrors the
# legacy src/pocketpaw/api/v1/connectors.py so the frontend's
# ``getConnectors()`` works unchanged when this handler shadows the
# runtime one in cloud deployments.

from __future__ import annotations

from fastapi import APIRouter, Depends

from ee.cloud.connectors import service as connectors_service
from ee.cloud.connectors.dto import (
    ConnectorDetailResponse,
    ConnectorResponse,
    EnableConnectorRequest,
    ExecuteActionRequest,
    ExecuteActionResponse,
    UpdateConnectorConfigRequest,
    WidgetRecipeResponse,
)
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id

# Mounted under /api/v1/cloud/connectors (not /api/v1/connectors) so it
# does NOT shadow the legacy pocket-scoped routes in
# src/pocketpaw/api/v1/connectors.py. The legacy routes (connect /
# disconnect / execute / status) remain the source of truth for
# pocket-bound connector instances; this cloud router owns the
# workspace-level enabled/disabled state used by the home widgets
# (and, eventually, automations and soul memory).
router = APIRouter(
    prefix="/cloud/connectors",
    tags=["Connectors"],
    dependencies=[Depends(require_license)],
)


@router.get("", response_model=list[ConnectorResponse])
async def list_connectors(
    workspace_id: str = Depends(current_workspace_id),
) -> list[ConnectorResponse]:
    """List all available connectors with this workspace's enabled state.

    Always returns the full registry catalog — disabled connectors carry
    ``enabled=false`` / ``status="disconnected"``. The frontend filters
    into "Connected" vs "Available" rails on its own.
    """
    return await connectors_service.list_connectors(workspace_id)


@router.get("/widget-recipes", response_model=list[WidgetRecipeResponse])
async def list_widget_recipes(
    workspace_id: str = Depends(current_workspace_id),
) -> list[WidgetRecipeResponse]:
    """Default home widgets every enabled connector contributes.

    Feeds the AddWidgetPicker's "From connectors" rail. Disabled
    connectors return zero recipes. Frontend compiles each recipe to a
    Ripple UISpec at render time.
    """
    return await connectors_service.list_widget_recipes(workspace_id)


@router.post("/{name}/execute", response_model=ExecuteActionResponse)
async def execute_action(
    name: str,
    body: ExecuteActionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> ExecuteActionResponse:
    """Execute one connector action with mode-aware dispatch.

    - ``cloud`` actions run in-process and return immediately.
    - ``local`` actions forward to the user's pocketpaw runtime via the
      chat WebSocket bus (PR-9 lands the listener; today returns 503
      ``connector.local_agent_unavailable``).
    - ``sandbox`` actions return 501 — reserved.
    """
    return await connectors_service.execute(workspace_id, name, body, user_id=user_id)


@router.get("/{name}", response_model=ConnectorDetailResponse)
async def get_connector(
    name: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectorDetailResponse:
    """Detail row for one connector — actions list + saved config."""
    return await connectors_service.get_connector(workspace_id, name)


@router.post("/{name}/enable", response_model=ConnectorResponse)
async def enable_connector(
    name: str,
    body: EnableConnectorRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectorResponse:
    """Enable a connector for this workspace.

    Idempotent — re-enabling an already-enabled connector simply updates
    the scope/config. The actual OAuth flow runs in
    ``api/v1/oauth_integrations.py``; this endpoint records the workspace's
    intent to use the connector and the scope it was granted at.
    """
    payload = body or EnableConnectorRequest()
    return await connectors_service.enable_connector(workspace_id, name, payload)


@router.post("/{name}/disable", response_model=ConnectorResponse)
async def disable_connector(
    name: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectorResponse:
    """Soft-disable a connector. Config + history survive."""
    return await connectors_service.disable_connector(workspace_id, name)


@router.patch("/{name}/config", response_model=ConnectorResponse)
async def update_config(
    name: str,
    body: UpdateConnectorConfigRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectorResponse:
    """Merge-patch the saved config for one connector."""
    return await connectors_service.update_config(workspace_id, name, body)
