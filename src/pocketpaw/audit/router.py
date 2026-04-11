# pocketpaw/audit/router.py — FastAPI router for the enterprise audit log API.
# Created: 2026-03-27
# Endpoints:
#   GET  /api/v1/audit                    — query entries with filters
#   GET  /api/v1/audit/export             — export as CSV or JSON for compliance
# Auth: requires "audit" scope (same pattern as memory/settings routers).

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from pocketpaw.audit.models import AuditEntry
from pocketpaw.audit.store import AuditStore, get_audit_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Audit"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AuditQueryResponse(BaseModel):
    entries: list[AuditEntry]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=AuditQueryResponse)
async def query_audit_log(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    category: str | None = Query(
        None, description="Filter by category: decision|data|config|security"
    ),
    actor: str | None = Query(None, description="Filter by actor (agent, user:name, system)"),
    date_from: datetime | None = Query(None, description="ISO datetime lower bound"),
    date_to: datetime | None = Query(None, description="ISO datetime upper bound"),
    limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
    store: AuditStore = Depends(get_audit_store),
) -> AuditQueryResponse:
    """Query the enterprise audit log with optional filters.

    Returns entries sorted newest-first. All parameters are optional.
    """
    entries = await store.query_entries(
        pocket_id=pocket_id,
        category=category,
        actor=actor,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    return AuditQueryResponse(entries=entries, total=len(entries))


@router.get("/audit/export")
async def export_audit_log(
    format: Literal["csv", "json"] = Query(..., description="Export format: csv or json"),
    pocket_id: str | None = Query(None),
    category: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    store: AuditStore = Depends(get_audit_store),
) -> Response:
    """Export audit log for compliance. Supports CSV and JSON formats.

    CSV is suitable for Excel/Google Sheets. JSON is suitable for programmatic
    processing or import into compliance tools.
    """
    if format == "csv":
        data = await store.export_csv(
            pocket_id=pocket_id,
            category=category,
            date_from=date_from,
            date_to=date_to,
        )
        filename = "audit_log.csv"
        if pocket_id:
            filename = f"audit_log_{pocket_id}.csv"
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    elif format == "json":
        data = await store.export_json(
            pocket_id=pocket_id,
            category=category,
            date_from=date_from,
            date_to=date_to,
        )
        return Response(
            content=data,
            media_type="application/json",
        )
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported format: {format}")
