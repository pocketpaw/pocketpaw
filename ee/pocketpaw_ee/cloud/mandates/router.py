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
# Updated: 2026-06-11 (UI contract sync 2) — added POST .../plan/resolve (the
# console's per-task gate action, mapped onto the real instinct approve-with-
# edits / reject paths) and the `patrols` senses toggles on create.
# Updated: 2026-06-11 (UI contract sync) — POST create returns {"mandate"},
# POST shift returns {"shift"}; the feedback route accepts BOTH the general
# {text, severity?, source} shape and the teaching {kind, reason, shift_no?,
# task_title?} shape (discriminated in the service on `kind`).

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
    """Create a standing mandate (admin-gated). Returns ``{"mandate": <detail>}``
    (UI contract envelope; the GET detail route stays unwrapped)."""
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
    body: dict[str, Any],
    _user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Intake patrol — file human feedback as a Sighting on the mandate.

    Two body shapes (UI contract), discriminated on the presence of ``kind``;
    the SERVICE validates the raw dict against the matching schema at entry
    (the validate-at-entry rule holds — discrimination just happens first):

    * GENERAL  — ``{text, severity?, source}`` → the sighting wire dict
      (autopilot and integrations keep using this).
    * TEACHING — ``{kind: reject|edit|plan, reason, shift_no?, task_title?}``
      → ``{"ok": true}`` (the gate UI's teaching channel)."""
    return await mandate_service.file_feedback(workspace_id, user_id, mandate_id, body)


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
    Instinct plan gate. Demo-bar manual trigger (admin-gated). Returns
    ``{"shift": {...}}`` (UI contract envelope)."""
    return await mandate_service.trigger_shift(workspace_id, user_id, mandate_id)


@router.post("/{mandate_id}/plan/resolve")
async def resolve_plan(
    mandate_id: str,
    body: dict[str, Any],
    user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """The console's authoritative gate action (UI contract): per-task verdicts
    for an in_gate shift plan → ``{"shift": {...}}``.

    The verdicts are MAPPED ONTO THE REAL INSTINCT PATH — the single chain
    authority — so the decision chain still closes exactly once in the same
    code the Tray uses:

    * any approved/edited subset → ``approve_action`` WITH EDITS (the blob's
      task list filtered + retitled; Corrections recorded; the plan executor
      dispatches the kept tasks as belt runs).
    * all tasks rejected → ``reject_action`` (router closes the chain; the
      shift records the rejection).

    Rejected tasks are recorded as teaching sightings (reason + task title) so
    the foreman's next digest learns. Decision ``index`` is 0-based into the
    plan's tasks array, and every task must carry exactly one decision."""
    orders = await mandate_service.prepare_plan_resolution(workspace_id, user_id, mandate_id, body)

    # Lazy import — the mandates router must not put a module-top dependency on
    # the instinct package (mirrors how the instinct router lazy-imports the
    # mandates executor on its approve path).
    from pocketpaw_ee.instinct.router import (
        ApproveRequest,
        RejectRequest,
        approve_action,
        reject_action,
    )

    if orders["mode"] == "reject":
        await reject_action(
            orders["action_id"],
            req=RejectRequest(reason=str(orders.get("reject_reason") or "")),
            user=user,
            workspace_id=workspace_id,
        )
    else:
        req = ApproveRequest(parameters=orders["parameters"]) if orders["edited"] else None
        await approve_action(
            orders["action_id"],
            req=req,
            user=user,
            workspace_id=workspace_id,
        )

    return {"shift": await mandate_service.shift_wire(workspace_id, orders["shift_id"])}


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
