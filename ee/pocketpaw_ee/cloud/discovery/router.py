# Discovery — FastAPI router (cloud 4-file rule).
# Created: 2026-06-21 (SZD finish slice F1 / feat/szd-finish-core) — the
#   workspace-discovery TRIGGER endpoint ``POST /cloud/discovery/run`` (mounted
#   at /api/v1/cloud/discovery via mount_cloud()). Mirrors connectors/router's
#   active-workspace dep chain: NO ``{workspace_id}`` path param — the workspace
#   resolves from ``current_workspace_id`` (the UI auto-threads X-Workspace-Id),
#   the user from ``current_user_id``. License-gated at the router level and
#   route-gated on ``connector.execute`` (the same action the connectors execute
#   route requires — discovery samples connectors, so it needs the same right).
#   Returns 202 Accepted: the run is fired in the background and the proposals
#   surface as pending Instinct Actions; the body carries the optimistic run_id.

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from pocketpaw_ee.cloud.discovery import service as discovery_service
from pocketpaw_ee.cloud.discovery.dto import DiscoveryRunRequest, DiscoveryRunResponse
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

router = APIRouter(
    prefix="/cloud/discovery",
    tags=["Discovery"],
    dependencies=[Depends(require_license)],
)


@router.post(
    "/run",
    response_model=DiscoveryRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_action_any_workspace("connector.execute"))],
)
async def run_discovery(
    body: DiscoveryRunRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> DiscoveryRunResponse:
    """Trigger a zero-setup discovery run for the active workspace.

    Enumerates the workspace's ENABLED connectors server-side (or honours an
    explicit ``connector_ids`` override), samples them, digests the exhaust into
    an ontology draft, and stages the gated proposals (fabric objects + starter
    pocket + any inferred governed rules) as pending Instinct Actions a human
    reviews in The Tray. Fires in the background and returns 202 immediately —
    the proposals appear in the pending-actions list the ApprovalsPanel polls.

    Gated by ``connector.execute`` (MEMBER+) in addition to the license check:
    discovery reads the workspace's connectors, so it requires the same right as
    executing a connector action.
    """
    result = await discovery_service.run(workspace_id, user_id, body or DiscoveryRunRequest())
    return DiscoveryRunResponse.model_validate(result)
