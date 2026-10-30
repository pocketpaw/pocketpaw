# router.py — FastAPI router for the Rules entity (governed Instinct rules).
# Created: 2026-07-09 (feat/instinct-guardrail-rules) — the thin HTTP surface over
# the shipped ``rules.service``. A UI-authored governed rule ("writes over $500 need
# approval") is created / listed / archived here, and the per-workspace authored-rule
# ENFORCEMENT toggle is read / set here. Every route is workspace-scoped (tenancy from
# the auth context, never a body/query) and admin-gated behind ``rules.manage``.
#
# Discipline (ee/cloud 4-file rules): NO enforcement logic and NO Beanie doc import
# live here — the router only maps HTTP <-> service calls. Errors surface as
# ``CloudError`` via the global handler (never ``HTTPException``). Mounted under
# ``/api/v1`` from ``ee/pocketpaw_ee/cloud/__init__.py``.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.rules import service as rules_service
from pocketpaw_ee.cloud.rules.dto import (
    CreateRuleRequest,
    EnforcementResponse,
    RuleResponse,
    SetEnforcementRequest,
)

router = APIRouter(
    prefix="/rules",
    tags=["Rules"],
    dependencies=[Depends(require_license)],
)


# ---------------------------------------------------------------------------
# Governed-rule CRUD (thin wrappers over rules.service).
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=RuleResponse,
    dependencies=[Depends(require_action_any_workspace("rules.manage"))],
)
async def create_rule(
    body: CreateRuleRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Create a governed rule for the caller's workspace.

    The service asserts ``body.draft.scope.workspace_id`` matches the caller's
    workspace, so an edited draft can never persist into another tenant.
    """
    return await rules_service.create_rule(workspace_id, user_id, body)


@router.get(
    "",
    response_model=list[RuleResponse],
    dependencies=[Depends(require_action_any_workspace("rules.manage"))],
)
async def list_rules(
    workspace_id: str = Depends(current_workspace_id),
) -> list[dict]:
    """List the ACTIVE governed rules for the caller's workspace (tenant-filtered;
    archived rows excluded)."""
    return await rules_service.get_active_rules(workspace_id)


# ---------------------------------------------------------------------------
# Per-workspace enforcement toggle. Declared BEFORE the ``/{rule_id}/archive``
# route so ``/enforcement`` is never captured as a rule id.
# ---------------------------------------------------------------------------


@router.get(
    "/enforcement",
    response_model=EnforcementResponse,
    dependencies=[Depends(require_action_any_workspace("rules.manage"))],
)
async def get_enforcement(
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Read the caller workspace's effective authored-rule enforcement state."""
    return await rules_service.get_enforcement(workspace_id)


@router.put(
    "/enforcement",
    response_model=EnforcementResponse,
    dependencies=[Depends(require_action_any_workspace("rules.manage"))],
)
async def set_enforcement(
    body: SetEnforcementRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Set (or clear, with ``enabled=null``) the caller workspace's tri-state
    enforcement override on the global flag."""
    return await rules_service.set_enforcement(workspace_id, user_id, body.enabled)


@router.post(
    "/{rule_id}/archive",
    response_model=RuleResponse,
    dependencies=[Depends(require_action_any_workspace("rules.manage"))],
)
async def archive_rule(
    rule_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Archive (retire) a governed rule. Tenant-scoped — a caller can only
    archive a rule in their own workspace; an unknown id 404s via CloudError."""
    return await rules_service.archive_rule(workspace_id, user_id, rule_id)
