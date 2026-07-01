# Connectors — FastAPI router.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Mounted at /api/v1/connectors via mount_cloud(). Wire shape mirrors the
# legacy src/pocketpaw/api/v1/connectors.py so the frontend's
# ``getConnectors()`` works unchanged when this handler shadows the
# runtime one in cloud deployments.
#
# Updated: 2026-05-06 (fix/rbac-connector-upload-guards) — added RBAC guards
# to all mutation endpoints. execute → connector.execute (MEMBER); enable /
# disable / config → connector.manage (ADMIN). Read-only routes (GET list,
# GET detail, GET widget-recipes) retain require_license only.
# Updated: 2026-06-08 (Phase B chunk 7) — added the member self-disconnect
#   endpoint POST /cloud/connectors/me/disconnect. It purges the CALLER's own
#   per-user Phase B data (KB scope, OAuth tokens, connector rows, ingest
#   state). Auth note: unlike the ADMIN-gated workspace-management mutations
#   (enable/disable/config — ``connector.manage``), this acts on the member's
#   OWN personal data, so it is MEMBER-level and hard-bound to ``current_user_
#   id``. The self-binding (member_id == caller) IS the protection — it mirrors
#   the chat-path KB gate's per-user scope binding, the correct sibling for a
#   per-user data operation. Admin-gating would wrongly block a member from
#   disconnecting their own accounts.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import (
    ConnectorDetailResponse,
    ConnectorResponse,
    DisconnectMemberResponse,
    EnableConnectorRequest,
    ExecuteActionRequest,
    ExecuteActionResponse,
    UpdateConnectorConfigRequest,
    WidgetRecipeResponse,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

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
    user_id: str = Depends(current_user_id),
) -> list[ConnectorResponse]:
    """List all available connectors with this workspace's enabled state.

    Filters results based on the user's connector permissions. Always returns
    the full registry catalog for users with full access; restricted users
    only see connectors they are allowed to use.
    """
    return await connectors_service.list_connectors(workspace_id, user_id=user_id)


@router.get("/widget-recipes", response_model=list[WidgetRecipeResponse])
async def list_widget_recipes(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[WidgetRecipeResponse]:
    """Default home widgets every enabled connector contributes.

    Feeds the AddWidgetPicker's "From connectors" rail. Disabled
    connectors return zero recipes. Frontend compiles each recipe to a
    Ripple UISpec at render time. Filtered by connector permissions.
    """
    return await connectors_service.list_widget_recipes(workspace_id, user_id=user_id)


@router.post(
    "/{name}/execute",
    response_model=ExecuteActionResponse,
    dependencies=[Depends(require_action_any_workspace("connector.execute"))],
)
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

    Gated by connector-level permissions in addition to RBAC.
    """
    return await connectors_service.execute(workspace_id, name, body, user_id=user_id)


@router.get("/{name}", response_model=ConnectorDetailResponse)
async def get_connector(
    name: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> ConnectorDetailResponse:
    """Detail row for one connector — actions list + saved config.

    Gated by connector-level permissions.
    """
    return await connectors_service.get_connector(workspace_id, name, user_id=user_id)


@router.post(
    "/{name}/enable",
    response_model=ConnectorResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def enable_connector(
    name: str,
    body: EnableConnectorRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> ConnectorResponse:
    """Enable a connector for this workspace.

    Idempotent — re-enabling an already-enabled connector simply updates
    the scope/config. The actual OAuth flow runs in
    ``api/v1/oauth_integrations.py``; this endpoint records the workspace's
    intent to use the connector and the scope it was granted at.
    Gated by connector-level permissions in addition to RBAC.
    """
    await connectors_service.assert_connector_allowed(workspace_id, user_id, name)
    payload = body or EnableConnectorRequest()
    return await connectors_service.enable_connector(workspace_id, name, payload)


@router.post(
    "/{name}/disable",
    response_model=ConnectorResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def disable_connector(
    name: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> ConnectorResponse:
    """Soft-disable a connector. Config + history survive.

    Gated by connector-level permissions in addition to RBAC.
    """
    await connectors_service.assert_connector_allowed(workspace_id, user_id, name)
    return await connectors_service.disable_connector(workspace_id, name)


@router.post("/me/disconnect", response_model=DisconnectMemberResponse)
async def disconnect_member(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> DisconnectMemberResponse:
    """Disconnect the CALLER's own per-user accounts and purge their data.

    A member ending their Phase B connection: deletes their private
    ``user:{id}`` KB scope (ingested mail/calendar), their per-user OAuth
    tokens, their per-user connector rows, and their ingest-state. Bound to the
    authenticated caller (``user_id`` IS the member id) so no member can ever
    purge another's data. Idempotent — re-disconnecting is a clean no-op.

    MEMBER-level by design (no ``connector.manage`` admin guard): a member must
    be able to disconnect their own personal accounts. See the module header.
    """
    result = await connectors_service.disconnect_member(workspace_id, user_id)
    return DisconnectMemberResponse(
        status=result["status"],
        scope=result["scope"],
        kb_cleared=result["kb_cleared"],
        tokens_deleted=result["tokens_deleted"],
        connectors_deleted=result["connectors_deleted"],
        ingest_state_deleted=result["ingest_state_deleted"],
    )


@router.patch(
    "/{name}/config",
    response_model=ConnectorResponse,
    dependencies=[Depends(require_action_any_workspace("connector.manage"))],
)
async def update_config(
    name: str,
    body: UpdateConnectorConfigRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> ConnectorResponse:
    """Merge-patch the saved config for one connector.

    Gated by connector-level permissions in addition to RBAC.
    """
    await connectors_service.assert_connector_allowed(workspace_id, user_id, name)
    return await connectors_service.update_config(workspace_id, name, body)
