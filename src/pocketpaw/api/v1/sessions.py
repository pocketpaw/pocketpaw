# Sessions router — list, delete, rename, search, export.
# Created: 2026-02-20
#
# Extracted from dashboard.py session endpoints.

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.common import StatusResponse
from pocketpaw.api.v1.schemas.sessions import (
    SessionListResponse,
    SessionSearchResponse,
    SessionSearchResult,
    SessionTitleRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sessions"], dependencies=[Depends(require_scope("sessions"))])


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(limit: int = Query(50, ge=1, le=500)):
    """List sessions using the fast session index."""
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "_load_session_index"):
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

    return {"sessions": [], "total": 0}


@router.delete("/sessions/{session_id}", response_model=StatusResponse)
async def delete_session(session_id: str):
    """Delete a session by ID."""
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "delete_session"):
        deleted = await store.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        return StatusResponse()

    raise HTTPException(status_code=501, detail="Store does not support session deletion")


@router.post("/sessions/{session_id}/title", response_model=StatusResponse)
async def update_session_title(session_id: str, body: SessionTitleRequest):
    """Update the title of a session."""
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "update_session_title"):
        updated = await store.update_session_title(session_id, body.title)
        if not updated:
            raise HTTPException(status_code=404, detail="Session not found")
        return StatusResponse()

    raise HTTPException(status_code=501, detail="Store does not support title updates")


@router.get("/sessions/search", response_model=SessionSearchResponse)
async def search_sessions(q: str = Query(""), limit: int = Query(20, ge=1, le=200)):
    """Search sessions by content."""
    from pocketpaw.memory import get_memory_manager

    if not q.strip():
        return SessionSearchResponse(sessions=[])

    query_lower = q.lower()
    manager = get_memory_manager()
    store = manager._store

    if not hasattr(store, "sessions_path"):
        return SessionSearchResponse(sessions=[])

    results: list[SessionSearchResult] = []
    index = store._load_session_index() if hasattr(store, "_load_session_index") else {}

    for session_file in store.sessions_path.glob("*.json"):
        if session_file.name.startswith("_") or session_file.name.endswith("_compaction.json"):
            continue
        try:
            data = json.loads(session_file.read_text())
            for msg in data:
                if query_lower in msg.get("content", "").lower():
                    safe_key = session_file.stem
                    meta = index.get(safe_key, {})
                    results.append(
                        SessionSearchResult(
                            id=safe_key,
                            title=meta.get("title", "Untitled"),
                            channel=meta.get("channel", "unknown"),
                            match=msg["content"][:200],
                            match_role=msg.get("role", ""),
                            last_activity=meta.get("last_activity", ""),
                        )
                    )
                    break
        except (json.JSONDecodeError, OSError):
            continue
        if len(results) >= limit:
            break

    return SessionSearchResponse(sessions=results)


@router.get("/sessions/{session_id}/history")
async def get_session_history(session_id: str, limit: int = Query(50, ge=1, le=500)):
    """Get session message history."""
    from pocketpaw.memory import get_memory_manager

    if not session_id:
        return []
    manager = get_memory_manager()
    return await manager.get_session_history(session_id, limit=limit)


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = Query("json")):
    """Export a session as downloadable JSON or Markdown."""
    from datetime import UTC, datetime

    from pocketpaw.memory import get_memory_manager

    if format not in ("json", "md"):
        raise HTTPException(status_code=400, detail="Format must be 'json' or 'md'")

    manager = get_memory_manager()
    entries = await manager._store.get_session(session_id)

    if not entries:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    if format == "json":
        messages = []
        for e in entries:
            ts = (
                e.created_at.isoformat()
                if hasattr(e.created_at, "isoformat")
                else str(e.created_at)
            )
            messages.append(
                {
                    "id": e.id,
                    "role": e.role or "user",
                    "content": e.content,
                    "timestamp": ts,
                    "metadata": e.metadata,
                }
            )
        content = json.dumps(
            {
                "export_version": "1.0",
                "exported_at": datetime.now(UTC).isoformat(),
                "session_id": session_id,
                "message_count": len(messages),
                "messages": messages,
            },
            indent=2,
            default=str,
        )
        media_type = "application/json"
        ext = "json"
    else:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        lines = [
            "# Conversation Export",
            f"**Session**: `{session_id}` | **Messages**: {len(entries)} | **Exported**: {now}",
            "",
            "---",
        ]
        for e in entries:
            role = (e.role or "user").capitalize()
            ts = ""
            if hasattr(e.created_at, "strftime"):
                ts = e.created_at.strftime("%H:%M")
            lines.append("")
            lines.append(f"**{role}** ({ts}):" if ts else f"**{role}**:")
            lines.append(e.content)
            lines.append("")
            lines.append("---")
        content = "\n".join(lines)
        media_type = "text/markdown"
        ext = "md"

    filename = f"pocketpaw-session-{session_id[:20]}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
