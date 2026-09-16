"""The ``/api/v1/platform`` router — every cross-tenant operator route.

Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.

THE NAMESPACE IS THE AUDIT BOUNDARY. Everything mounted here is cross-tenant by
definition and takes its target as an explicit parameter, inverting the rule
that holds everywhere else in this codebase ("workspace comes from the session,
never from the caller"). Two obligations follow, and both are asserted by
``tests/cloud/platform/test_platform_guard.py`` rather than left to review:

  1. Every route here carries ``require_platform(...)``.
  2. No route OUTSIDE here accepts a caller-supplied workspace id as a path
     parameter.

Chunk 1 ships the two routes it owns: the operator's own identity, and the
operator audit trail. Tenant, credits, entitlement, stats, revenue, model and
settings routes arrive in their own chunks, each as a sub-router mounted here.

Changed 2026-09-16 (feat/platform-credits, chunk 6): mounted ``credits_router``
— the wallet read, ledger-history read, adjust and reconcile routes at
``/workspaces/{workspace_id}/credits*``. Sorted alphabetically by module name
alongside its siblings (credits < users < workspaces).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform.credits import router as credits_router
from pocketpaw_ee.cloud.platform.health import router as health_router
from pocketpaw_ee.cloud.platform.settings import router as settings_router
from pocketpaw_ee.cloud.platform.users import router as users_router
from pocketpaw_ee.cloud.platform.workspaces import router as workspaces_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform", tags=["platform"])

# Sub-routers, one per operator surface. Each mounts UNDER the platform prefix,
# so the guard-coverage test in tests/cloud/platform/test_platform_guard.py
# reaches their routes too — a sub-router that forgot require_platform fails
# there rather than shipping.
router.include_router(credits_router)
router.include_router(health_router)
router.include_router(settings_router)
router.include_router(users_router)
router.include_router(workspaces_router)


class PlatformIdentityOut(BaseModel):
    """Who the console is talking to, and what it may do."""

    user_id: str
    email: str
    platform_role: str
    # The action keys this rung satisfies. The console uses it to hide controls
    # it would only get a 403 from. It is a convenience, not a boundary — the
    # boundary is require_platform on each route.
    allowed_actions: list[str]


class PlatformAuditOut(BaseModel):
    id: str
    actor_id: str
    actor_email: str
    actor_platform_role: str
    action: str
    target_type: str
    target_workspace: str | None
    target_user: str | None
    reason: str
    status: str
    before: dict
    after: dict
    ip: str | None
    at: datetime


@router.get("/me", response_model=PlatformIdentityOut)
async def platform_identity(
    operator: Annotated[User, Depends(require_platform("platform.audit.read"))],
) -> PlatformIdentityOut:
    """The caller's platform identity.

    Gated on the lowest read rung, so it answers for support and operator alike.
    A caller with no platform rung gets 403 here, which is what the console uses
    to tell "signed in, not an operator" apart from "not signed in".
    """
    from pocketpaw_ee.guards.platform import PLATFORM_ACTIONS, check_platform_action
    from pocketpaw_ee.guards.rbac import Forbidden

    allowed: list[str] = []
    for action in PLATFORM_ACTIONS:
        try:
            check_platform_action(action, operator.platform_role)
        except Forbidden:
            continue
        allowed.append(action)

    return PlatformIdentityOut(
        user_id=str(operator.id),
        email=operator.email or "",
        platform_role=operator.platform_role or "",
        allowed_actions=sorted(allowed),
    )


@router.get("/audit", response_model=list[PlatformAuditOut])
async def list_platform_audit(
    operator: Annotated[User, Depends(require_platform("platform.audit.read"))],
    workspace_id: Annotated[str | None, Query(description="Filter to one tenant")] = None,
    actor_id: Annotated[str | None, Query(description="Filter to one operator")] = None,
    before: Annotated[
        datetime | None, Query(description="Cursor: return events strictly older than this")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PlatformAuditOut]:
    """The operator action trail, newest first.

    Cursor is a timestamp rather than an offset because the collection only ever
    grows at the head; an offset would re-read shifting pages. ``before`` is
    strict, so a row cannot be returned twice — but note that two rows written
    inside the same clock tick share a timestamp, and a strict cursor landing
    exactly between them will skip the second. Ties are unlikely here (operator
    writes are human-paced) and the alternative is a compound cursor this chunk
    does not need yet.
    """
    query: dict = {}
    if workspace_id:
        query["target_workspace"] = workspace_id
    if actor_id:
        query["actor_id"] = actor_id
    if before:
        query["at"] = {"$lt": before}

    events = (
        await PlatformAuditEvent.find(query).sort("-at").limit(limit).to_list()
        if query
        else await PlatformAuditEvent.find_all().sort("-at").limit(limit).to_list()
    )

    return [
        PlatformAuditOut(
            id=str(event.id),
            actor_id=event.actor_id,
            actor_email=event.actor_email,
            actor_platform_role=event.actor_platform_role,
            action=event.action,
            target_type=event.target_type,
            target_workspace=event.target_workspace,
            target_user=event.target_user,
            reason=event.reason,
            status=event.status,
            before=event.before,
            after=event.after,
            ip=event.ip,
            at=event.at,
        )
        for event in events
    ]


__all__ = ["router"]
