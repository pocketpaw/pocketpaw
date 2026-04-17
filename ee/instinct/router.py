# ee/instinct/router.py — FastAPI router for the Instinct decision pipeline API.
# Created: 2026-03-28 — Propose, approve/reject, list pending, query audit.
# Updated: 2026-03-30 — Added GET /instinct/actions (list all with status filter),
#   GET /instinct/audit/export (JSON export), switched to singleton from ee.api.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from ee.instinct.models import (
    Action,
    ActionCategory,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditEntry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Instinct"])


def _store():
    from ee.api import get_instinct_store

    return get_instinct_store()


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ProposeRequest(BaseModel):
    pocket_id: str
    title: str
    description: str = ""
    recommendation: str = ""
    trigger: ActionTrigger
    category: ActionCategory = ActionCategory.WORKFLOW
    priority: ActionPriority = ActionPriority.MEDIUM
    parameters: dict[str, Any] = {}


class RejectRequest(BaseModel):
    reason: str = ""


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ActionsListResponse(BaseModel):
    actions: list[Action]
    total: int


class AuditListResponse(BaseModel):
    entries: list[AuditEntry]
    total: int


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


@router.post("/instinct/actions", response_model=Action, status_code=201)
async def propose_action(req: ProposeRequest):
    """Propose a new action for human approval."""
    return await _store().propose(
        pocket_id=req.pocket_id,
        title=req.title,
        description=req.description,
        recommendation=req.recommendation,
        trigger=req.trigger,
        category=req.category,
        priority=req.priority,
        parameters=req.parameters,
    )


@router.get("/instinct/actions/pending", response_model=list[Action])
async def pending_actions(pocket_id: str | None = Query(None)):
    """List actions waiting for human approval."""
    return await _store().pending(pocket_id=pocket_id)


@router.get("/instinct/actions", response_model=ActionsListResponse)
async def list_actions(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    status: str | None = Query(
        None, description="Filter by status: pending|approved|rejected|executed|failed"
    ),
    limit: int = Query(50, ge=1, le=500, description="Max actions to return"),
):
    """List all actions with optional status and pocket filters."""
    store = _store()
    status_enum = ActionStatus(status) if status else None
    actions = await store.list_actions(
        pocket_id=pocket_id,
        status=status_enum,
        limit=limit,
    )
    return ActionsListResponse(actions=actions, total=len(actions))


@router.post("/instinct/actions/{action_id}/approve", response_model=Action)
async def approve_action(action_id: str):
    """Approve a pending action."""
    action = await _store().approve(action_id)
    if not action:
        raise HTTPException(404, "Action not found")
    return action


@router.post("/instinct/actions/{action_id}/reject", response_model=Action)
async def reject_action(action_id: str, req: RejectRequest | None = None):
    """Reject a pending action with an optional reason."""
    reason = req.reason if req else ""
    action = await _store().reject(action_id, reason=reason)
    if not action:
        raise HTTPException(404, "Action not found")
    return action


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------


@router.get("/instinct/audit", response_model=AuditListResponse)
async def query_audit(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    category: str | None = Query(
        None, description="Filter by category: decision|data|config|security"
    ),
    event: str | None = Query(None, description="Filter by event type"),
    limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
):
    """Query instinct audit log entries with optional filters."""
    entries = await _store().query_audit(
        pocket_id=pocket_id,
        category=category,
        event=event,
        limit=limit,
    )
    return AuditListResponse(entries=entries, total=len(entries))


@router.get("/instinct/audit/export")
async def export_audit(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
):
    """Export the full instinct audit log as JSON for compliance."""
    data = await _store().export_audit(pocket_id=pocket_id)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="instinct_audit.json"'},
    )
