# router.py — FastAPI router for the automations-status entity (C3).
# Created: 2026-07-11 (feat/external-alerting-c2c3) — the thin HTTP surface over
# ``automations_status.service``. GET /automations/status returns the aggregate
# (OSS rules + evaluator status + cloud sweep registry + per-workspace enable
# state); PUT /automations/config flips the per-workspace opt-out.
#
# Discipline (ee/cloud 4-file rules): NO business logic and NO Beanie doc import
# live here — the router only maps HTTP <-> service calls. Every route is
# workspace-scoped (tenancy from the auth context, never a body/query). Errors
# surface as ``CloudError`` via the global handler (never ``HTTPException``).
# Mounted under ``/api/v1`` from ``ee/pocketpaw_ee/cloud/__init__.py``.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import (
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.automations_status import service as automations_status_service
from pocketpaw_ee.cloud.automations_status.dto import (
    AutomationStatusResponse,
    SetWorkspaceAutomationRequest,
    WorkspaceAutomationStateOut,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/automations",
    tags=["Automations"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "/status",
    response_model=AutomationStatusResponse,
    dependencies=[Depends(require_action_any_workspace("automations.read"))],
)
async def get_automation_status(
    ctx: RequestContext = Depends(request_context),
) -> AutomationStatusResponse:
    """Aggregate automation status for the caller's active workspace."""
    return await automations_status_service.agent_get_status(ctx)


@router.put(
    "/config",
    response_model=WorkspaceAutomationStateOut,
    dependencies=[Depends(require_action_any_workspace("automations.manage"))],
)
async def set_automation_config(
    body: SetWorkspaceAutomationRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> WorkspaceAutomationStateOut:
    """Upsert the per-workspace automation opt-out (sweeps / automations)."""
    state = await automations_status_service.set_workspace_config(
        workspace_id,
        sweeps_enabled=body.sweeps_enabled,
        automations_enabled=body.automations_enabled,
    )
    return WorkspaceAutomationStateOut(
        workspace_id=state.workspace_id,
        sweeps_enabled=state.sweeps_enabled,
        automations_enabled=state.automations_enabled,
        configured=state.configured,
    )
