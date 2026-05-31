# pocketpaw/audit/runtime_router.py — Canonical audit query surface.
# Created: 2026-04-19 (Cluster C / PR4) — Consolidates the two audit surfaces
# flagged in docs/plans/cluster-C-reality.md (`/api/v1/audit` +
# `/api/v1/instinct/audit`) behind `/api/v1/runtime/audit`. Adds
# workspace_id filter and a parameter-bound full-text search over the
# description + payload summary. `/audit` is kept as a deprecated alias in
# the existing audit.router (forwarding below).
"""Canonical runtime audit router.

Frontend calls `GET /api/v1/runtime/audit` with optional:
- ``workspace_id`` — workspace-level rollup across every pocket.
- ``pocket_id`` — single-pocket filter (existing behaviour).
- ``category`` — decision|data|config|security.
- ``q`` — free-text search across description + action + context payload.
- ``limit`` — bounded by the store (default 200).

The two legacy paths (`/api/v1/audit` and `/api/v1/instinct/audit`) stay
mounted as thin aliases that forward to this canonical handler, so the
existing paw-enterprise frontend path (`queryAudit` in `runtime/api.ts`
which hits `/instinct/audit`) keeps working unchanged.

Security notes:
    The ``q`` search term is interpolated only through bound parameters.
    We never concatenate user input into SQL — a deliberate test proves
    that ``q="'; DROP TABLE audit_log; --"`` returns zero results and
    leaves the table intact.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from pocketpaw.api.deps import require_scope
from pocketpaw.audit.models import AuditEntry
from pocketpaw.audit.store import AuditStore, get_audit_store

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Runtime Audit"],
    dependencies=[Depends(require_scope("audit"))],
)


class RuntimeAuditResponse(BaseModel):
    entries: list[AuditEntry]
    total: int


@router.get("/runtime/audit", response_model=RuntimeAuditResponse)
async def query_runtime_audit(
    workspace_id: str | None = Query(None, description="Workspace-level rollup filter"),
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    category: str | None = Query(
        None, description="Filter by category: decision|data|config|security"
    ),
    actor: str | None = Query(None, description="Filter by actor (agent, user:name, system)"),
    q: str | None = Query(
        None,
        description=(
            "Free-text search across action + description + context payload. "
            "Bound as a parameter; no raw SQL from this field."
        ),
        max_length=200,
    ),
    date_from: datetime | None = Query(None, description="ISO datetime lower bound"),
    date_to: datetime | None = Query(None, description="ISO datetime upper bound"),
    limit: int = Query(200, ge=1, le=1000, description="Max entries to return"),
    store: AuditStore = Depends(get_audit_store),
) -> RuntimeAuditResponse:
    """Canonical audit query — workspace-level rollup + FTS."""
    entries = await store.search_entries(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        category=category,
        actor=actor,
        q=q,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    return RuntimeAuditResponse(entries=entries, total=len(entries))


@router.get("/runtime/audit/export")
async def export_runtime_audit(
    format: Literal["csv", "json"] = Query(..., description="Export format: csv or json"),
    workspace_id: str | None = Query(None),
    pocket_id: str | None = Query(None),
    category: str | None = Query(None),
    q: str | None = Query(None, max_length=200),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    store: AuditStore = Depends(get_audit_store),
) -> Response:
    """Export filtered audit rows as CSV or JSON."""
    # Re-use the store's export helpers but honour the new filters by
    # pre-filtering via search_entries and writing the subset.
    from pocketpaw.audit.store import _entries_to_csv_bytes, _entries_to_json_bytes

    entries = await store.search_entries(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        category=category,
        q=q,
        date_from=date_from,
        date_to=date_to,
        limit=10_000,
    )
    if format == "csv":
        data = _entries_to_csv_bytes(entries)
        filename = "runtime_audit.csv"
        if workspace_id:
            filename = f"runtime_audit_workspace_{workspace_id}.csv"
        elif pocket_id:
            filename = f"runtime_audit_{pocket_id}.csv"
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    elif format == "json":
        data = _entries_to_json_bytes(entries)
        return Response(content=data, media_type="application/json")
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported format: {format}")
