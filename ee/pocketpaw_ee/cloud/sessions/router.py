"""Sessions domain — FastAPI router.

Updated 2026-08-01: the three ``runtime`` / ``touch`` routes below now resolve
identity through the same dependencies as every other route in this router, and
their reads and writes are scoped to that identity. They were the last routes
here relying on an in-handler decision instead. The invariants are pinned in
``tests/cloud/sessions/test_runtime_route_auth.py``; the standing per-route
audit lives in ``tests/cloud/auth/test_route_auth_audit.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from starlette.responses import Response

from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import (
    CreateSessionRequest,
    SessionPage,
    Surface,
    UpdateSessionRequest,
    session_to_wire_dict,
)
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

router = APIRouter(prefix="/sessions", tags=["Sessions"], dependencies=[Depends(require_license)])

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def create_session(
    body: CreateSessionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    ctx = sessions_service.legacy_ctx(user_id, workspace_id)
    session = await sessions_service.create(ctx, workspace_id, body)
    return session_to_wire_dict(session)


@router.get("", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def list_sessions(
    agent_id: str | None = None,
    surface: Surface | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    """List the user's sessions.

    Query params:
    - ``agent_id`` filters to DM sessions for that agent (used by the
      frontend to resolve the DM room).
    - ``surface`` filters to sessions stamped with the given originating
      surface (``chat`` / ``files`` / ``pocket_creation``). Omitted →
      every row, including legacy ``surface=None`` rows.
    """
    ctx = sessions_service.legacy_ctx(user_id, workspace_id)
    if agent_id:
        items = await sessions_service.list_by_agent(ctx, workspace_id, agent_id)
    else:
        items = await sessions_service.list_for_owner(ctx, workspace_id, surface=surface)
    return [session_to_wire_dict(s) for s in items]


# ---------------------------------------------------------------------------
# Per-mode listing — one keyset-paginated endpoint per chat surface. Each
# mode's rail paginates independently so a workspace with 95 chat threads and
# 100+ pocket chats never merges them into one list. Surface scoping follows
# the legacy-null-as-chat rule (handled in the service). These literal routes
# MUST stay above ``GET /{session_id}`` so they aren't captured as an id.
# ---------------------------------------------------------------------------


async def _mode_page(
    surface: str,
    cursor: str | None,
    limit: int,
    workspace_id: str,
    user_id: str,
) -> SessionPage:
    ctx = sessions_service.legacy_ctx(user_id, workspace_id)
    rows, next_cursor = await sessions_service.list_for_owner_page(
        ctx, workspace_id, surface=surface, cursor=cursor, limit=limit
    )
    return SessionPage(
        sessions=[session_to_wire_dict(s) for s in rows],
        nextCursor=next_cursor,
    )


@router.get("/chat", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def list_chat_sessions(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> SessionPage:
    """Chat-surface sessions (includes legacy ``surface=None`` rows)."""
    return await _mode_page("chat", cursor, limit, workspace_id, user_id)


@router.get("/files", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def list_files_sessions(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> SessionPage:
    """Files-surface sessions only."""
    return await _mode_page("files", cursor, limit, workspace_id, user_id)


@router.get("/foresight", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def list_foresight_sessions(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> SessionPage:
    """Foresight-surface sessions only."""
    return await _mode_page("foresight", cursor, limit, workspace_id, user_id)


@router.get(
    "/pocket-creation",
    dependencies=[Depends(require_action_any_workspace("session.read_own"))],
)
async def list_pocket_creation_sessions(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> SessionPage:
    """Pocket-creation-surface sessions only."""
    return await _mode_page("pocket_creation", cursor, limit, workspace_id, user_id)


@router.get("/runtime", dependencies=[Depends(require_action_any_workspace("session.read_own"))])
async def list_runtime_sessions(
    limit: int = 50,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """List the CALLER's sessions from the active memory store's index.

    Dispatches on the store: MongoMemoryStore exposes an async variant,
    FileMemoryStore a sync one. Stores without either return empty.

    Guard and scope both added 2026-08-01, and both are load-bearing:
    authenticating the caller without also scoping the store query would still
    answer with rows that are not theirs. The guard mirrors the sibling listing
    routes above so every listing in this router answers "the caller's own
    sessions" the same way, and the store query carries the matching workspace
    and owner filter.
    """
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "_load_session_index_async"):
        index = await store._load_session_index_async(workspace_id=workspace_id, owner_id=user_id)
    elif hasattr(store, "_load_session_index"):
        # The file store is single-tenant by construction — it is the OSS /
        # dedicated-install backend, and a cloud deployment cannot reach this
        # branch (``verify_cloud_memory_backend`` refuses to boot on anything
        # but MongoMemoryStore). So there is no tenant to scope to here.
        index = store._load_session_index()
    else:
        return {"sessions": [], "total": 0}

    entries = sorted(
        index.items(),
        key=lambda kv: kv[1].get("last_activity", ""),
        reverse=True,
    )[:limit]

    sessions = [{"id": safe_key, **meta} for safe_key, meta in entries]

    return {"sessions": sessions, "total": len(index)}


@router.post("/runtime/create")
async def create_runtime_session(
    user_id: str = Depends(current_user_id),  # noqa: ARG001 — identity gate, not input
) -> dict:
    """Create a new runtime session (no MongoDB — just a session key).

    The dependency is unused by the body on purpose: this handler mints a
    random key and touches nothing, so there is nothing here to scope. It
    requires a session anyway, because "needs no session but is currently
    harmless" stops holding the moment somebody makes this handler persist
    something, and that change would not look like a security change to
    whoever writes it. Only ``current_user_id`` — no workspace is needed to
    generate a string.
    """
    import uuid

    safe_key = f"websocket_{uuid.uuid4().hex[:12]}"
    return {"id": safe_key, "title": "New Chat"}


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    ctx = sessions_service.legacy_ctx(user_id)
    return session_to_wire_dict(await sessions_service.get(ctx, session_id))


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    body: UpdateSessionRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    ctx = sessions_service.legacy_ctx(user_id)
    return session_to_wire_dict(await sessions_service.update(ctx, session_id, body))


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    ctx = sessions_service.legacy_ctx(user_id)
    await sessions_service.delete(ctx, session_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# History proxy & activity tracking
# ---------------------------------------------------------------------------


@router.get("/{session_id}/history")
async def get_session_history(
    session_id: str,
    limit: int = 50,
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return session history from the unified Mongo messages store."""
    from pocketpaw_ee.cloud.shared.errors import NotFound

    try:
        return await sessions_service.get_history(session_id, user_id, limit=limit)
    except NotFound:
        return {"messages": []}


@router.post("/{session_id}/touch", status_code=204)
async def touch_session(
    session_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    """Bump a session's activity — the caller's own sessions only.

    Both the dependency and the ``user_id`` argument are new (2026-08-01).
    Ownership is the requirement here, not merely a session: this writes
    ``lastActivity`` / ``messageCount`` and emits a ``SessionUpdated`` onto the
    OWNER's realtime feed, so a caller who cannot read the session must not be
    able to move it either.
    """
    await sessions_service.touch(session_id, user_id)
    return Response(status_code=204)
