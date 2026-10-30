# ee/pocketpaw_ee/cloud/temporal_sweeps/router.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — REST surface
# for inspecting the RFC 03 v2 temporal-sweep state matrix. The single
# route is a debug / dashboard read-only endpoint:
# ``GET /pockets/{id}/temporal-sweeps/state`` returns every persisted
# (trigger, row) truth value for that pocket scoped to the caller's
# workspace. Wave 3d does not surface a manual "sweep now" route — that
# is explicitly out of scope (deferred to a follow-up PR).
#
# Routes are thin: parse, delegate to the service, return what the
# service produced. Errors propagate via ``CloudError``; the central
# ``cloud_error_handler`` maps to JSON. Never raises ``HTTPException``
# (rule 10).

"""FastAPI router for ``temporal_sweeps``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

router = APIRouter(prefix="/pockets", tags=["TemporalSweeps"])


def _require_workspace(ctx: RequestContext) -> str:
    if not ctx.workspace_id:
        raise ValidationError(
            "temporal_sweep.workspace_required",
            "no active workspace on this request",
        )
    return ctx.workspace_id


@router.get("/{pocket_id}/temporal-sweeps/state")
async def list_state(
    pocket_id: str,
    limit: int = 500,
    ctx: RequestContext = Depends(request_context),
) -> list[dict]:
    """Return the persisted (trigger, row) state rows for one pocket.

    Tenant-filtered. Useful for dashboards + debugging — operators can
    see why a temporal trigger did or did not fire on the last sweep
    (the persisted ``predicate_value`` is the value the next sweep will
    diff against).
    """
    workspace_id = _require_workspace(ctx)
    return await sweeps_service.list_state_for_pocket(
        workspace_id,
        ctx.user_id,
        pocket_id,
        limit=limit,
    )


__all__ = ["router"]
