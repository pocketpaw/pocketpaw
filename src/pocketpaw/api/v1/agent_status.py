from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from pocketpaw.api.v1.schemas.status import AgentStatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Status"])

_DEBOUNCE_MS = 1000


def _snapshot_fingerprint(snap: dict) -> tuple:
    """Extract a comparable fingerprint from a snapshot (ignores timing fields)."""
    global_state = snap["global"]["state"]
    sessions = tuple(
        (s["session_key"], s["state"], s["tool_name"]) for s in snap.get("sessions", [])
    )
    return (global_state, sessions)


def _get_status_api_key() -> str:
    """Return the configured status API key (cached after first call)."""
    cached = getattr(_get_status_api_key, "_value", None)
    if cached is not None:
        return cached
    from pocketpaw.config import Settings

    key = Settings.load().status_api_key
    _get_status_api_key._value = key  # type: ignore[attr-defined]
    return key


def _check_status_key(request: Request, key: str | None = None) -> None:
    """Validate optional status API key from header or query param."""
    expected = _get_status_api_key()
    if not expected:
        return  # No key configured, allow all

    provided = key or request.headers.get("x-status-key", "")
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing status API key")


@router.get("/agent/status", response_model=AgentStatusResponse)
async def get_agent_status(request: Request, key: str | None = Query(None)):
    """Return current agent state: global status and per-session breakdown.

    Authenticate with ``X-Status-Key`` header or ``?key=`` query param
    if ``POCKETPAW_STATUS_API_KEY`` is set.
    """
    _check_status_key(request, key)

    from pocketpaw.dashboard_state import status_tracker

    return status_tracker.snapshot()


@router.get("/agent/status/stream")
async def agent_status_stream(request: Request, key: str | None = Query(None)):
    """SSE stream of agent state changes.

    Sends a full snapshot on connect and on every state change.
    Debounced at 200ms to avoid flooding during rapid tool sequences.
    """
    _check_status_key(request, key)

    from pocketpaw.dashboard_state import status_tracker

    async def _event_generator():
        try:
            # Send initial snapshot immediately
            snap = status_tracker.snapshot()
            last_version = status_tracker.version
            last_fp = _snapshot_fingerprint(snap)
            yield f"event: status\ndata: {json.dumps(snap)}\n\n"

            while True:
                if await request.is_disconnected():
                    return
                changed = await status_tracker.wait_for_change(
                    since_version=last_version, timeout=30.0
                )
                if changed:
                    # Debounce: wait for rapid successive events to settle
                    await asyncio.sleep(_DEBOUNCE_MS / 1000)
                    snap = status_tracker.snapshot()
                    last_version = status_tracker.version
                    fp = _snapshot_fingerprint(snap)
                    # Skip sending if state hasn't meaningfully changed
                    if fp != last_fp:
                        last_fp = fp
                        yield f"event: status\ndata: {json.dumps(snap)}\n\n"
                else:
                    # Keepalive every 30s
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
