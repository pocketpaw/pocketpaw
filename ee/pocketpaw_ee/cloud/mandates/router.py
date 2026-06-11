# ee/pocketpaw_ee/cloud/mandates/router.py
# Created: 2026-06-11 (feat/belt-mandates, slice 1 — models + CRUD).
#
# FastAPI router for the MANDATE primitive — the standing Belt JOB. Routes ride
# the ``/belt/mandates`` prefix (the spec pins them under the belt surface). The
# routes are THIN: they read identity (workspace + user) from the cloud deps,
# delegate to ``ee.cloud.mandates.service``, and return the wire dict the service
# built. RBAC mirrors the belt console — ``belt.read`` (MEMBER) on reads,
# ``belt.manage`` (ADMIN) on mutations (create / shift trigger / feedback intake).
# Errors propagate via ``CloudError`` so the central handler maps them; the
# router never raises ``HTTPException``.
#
# Updated: 2026-06-11 (slice 2 — patrols) — added feedback intake +
# sightings read.
# Updated: 2026-06-11 (slice 4 — plan gate) — added POST .../shift.
# Updated: 2026-06-11 (slice 5 — pawprints) — added GET .../pawprints.

"""FastAPI router for Belt mandates (standing jobs)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.mandates import service as mandate_service
from pocketpaw_ee.cloud.mandates.dto import (
    CreateMandateRequest,
    FeedbackRequest,
)

router = APIRouter(
    prefix="/belt/mandates", tags=["Belt Mandates"], dependencies=[Depends(require_license)]
)


@router.post("")
async def create_mandate(
    body: CreateMandateRequest,
    _user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Create a standing mandate (admin-gated). Returns the mandate detail."""
    return await mandate_service.create_mandate(workspace_id, user_id, body.model_dump())


@router.get("")
async def list_mandates(
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """List the workspace's mandates with a per-mandate health summary."""
    return await mandate_service.list_mandates(workspace_id, user_id)


@router.get("/{mandate_id}")
async def get_mandate(
    mandate_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Return one mandate's detail (charter, recent shifts, sightings-by-patrol).
    A mandate in another workspace is a 404."""
    return await mandate_service.get_mandate(workspace_id, user_id, mandate_id)


@router.post("/{mandate_id}/feedback")
async def file_feedback(
    mandate_id: str,
    body: FeedbackRequest,
    _user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Intake patrol — file human feedback as a Sighting on the mandate."""
    return await mandate_service.file_feedback(workspace_id, user_id, mandate_id, body.model_dump())


@router.get("/{mandate_id}/sightings")
async def list_sightings(
    mandate_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """List a mandate's sightings, newest-first."""
    return await mandate_service.list_sightings(workspace_id, user_id, mandate_id)


@router.post("/{mandate_id}/shift")
async def trigger_shift(
    mandate_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Run a SHIFT — the foreman plans a few tasks; the plan routes through the
    Instinct plan gate. Demo-bar manual trigger (admin-gated)."""
    return await mandate_service.trigger_shift(workspace_id, user_id, mandate_id)


@router.get("/{mandate_id}/pawprints")
async def get_pawprints(
    mandate_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Return the mandate's past-tense event feed (shift n: proposed / approved /
    rejected / executed / stood_down, with evidence refs)."""
    return await mandate_service.get_pawprints(workspace_id, user_id, mandate_id)


__all__ = ["router"]
