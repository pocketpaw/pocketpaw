"""Sessions domain — FastAPI router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.cloud.license import require_license
from ee.cloud.sessions.schemas import (
    CreateSessionRequest,
    UpdateSessionRequest,
)
from ee.cloud.sessions.service import SessionService
from ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(
    prefix="/sessions", tags=["Sessions"], dependencies=[Depends(require_license)]
)

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("")
async def create_session(
    body: CreateSessionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await SessionService.create(workspace_id, user_id, body)


@router.get("")
async def list_sessions(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    return await SessionService.list_sessions(workspace_id, user_id)


@router.get("/runtime")
async def list_runtime_sessions(limit: int = 50) -> dict:
    """List sessions from PocketPaw's native runtime file store."""
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if not hasattr(store, "_load_session_index"):
        return {"sessions": [], "total": 0}

    index = store._load_session_index()
    entries = sorted(
        index.items(),
        key=lambda kv: kv[1].get("last_activity", ""),
        reverse=True,
    )[:limit]

    sessions = []
    for safe_key, meta in entries:
        sessions.append({"id": safe_key, **meta})

    return {"sessions": sessions, "total": len(index)}


@router.post("/runtime/create")
async def create_runtime_session() -> dict:
    """Create a new runtime session (no MongoDB — just a session key)."""
    import uuid

    safe_key = f"websocket_{uuid.uuid4().hex[:12]}"
    return {"id": safe_key, "title": "New Chat"}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await SessionService.get(session_id, user_id)


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    body: UpdateSessionRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await SessionService.update(session_id, user_id, body)


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await SessionService.delete(session_id, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# History proxy & activity tracking
# ---------------------------------------------------------------------------


@router.get("/{session_id}/history")
async def get_session_history(
    session_id: str,
    limit: int = 50,
) -> dict:
    """Get session history. Tries MongoDB session first, falls back to runtime."""
    # Try runtime directly (handles both general and pocket sessions)
    try:
        from pocketpaw.memory import get_memory_manager

        manager = get_memory_manager()
        sid = session_id
        for key in [sid, sid.replace("_", ":", 1), f"websocket:{sid}"]:
            try:
                entries = await manager.get_session_history(key, limit=limit)
                if entries:
                    return {"messages": entries}
            except Exception:
                continue
    except Exception:
        pass

    return {"messages": []}


@router.post("/{session_id}/touch", status_code=204)
async def touch_session(session_id: str) -> Response:
    await SessionService.touch(session_id)
    return Response(status_code=204)
