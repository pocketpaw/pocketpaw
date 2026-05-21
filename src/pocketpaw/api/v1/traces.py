# Trace API router — list and inspect request traces.

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query

from pocketpaw.api.deps import require_scope
from pocketpaw.traces import get_trace_store

router = APIRouter(tags=["Traces"])

# Characters that could form path-traversal sequences inside a session_id value.
_UNSAFE_SESSION_RE = re.compile(r"[/\\]|\.\.")


def _sanitize_session_id(value: str) -> str:
    """Reject session_id values that look like path-traversal attempts."""
    if value and _UNSAFE_SESSION_RE.search(value):
        raise HTTPException(
            status_code=400,
            detail="session_id contains invalid characters",
        )
    return value


@router.get("/traces", dependencies=[Depends(require_scope("metrics", "admin"))])
async def list_traces(
    since: str = Query("", description="ISO timestamp filter"),
    limit: int = Query(100, ge=1, le=1000),
    session_id: str = Query("", description="session key or session id"),
    min_cost: float = Query(0.0, ge=0.0),
):
    """Return recent traces with optional filters."""
    store = get_trace_store()
    return await store.list_traces(
        since=since or None,
        limit=limit,
        session_id=_sanitize_session_id(session_id),
        min_cost=min_cost,
    )


@router.get("/traces/{trace_id}", dependencies=[Depends(require_scope("metrics", "admin"))])
async def get_trace(trace_id: str):
    """Return full trace payload by trace ID."""
    store = get_trace_store()
    trace = await store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace
