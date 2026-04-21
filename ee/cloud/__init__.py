"""PocketPaw Enterprise Cloud — domain-driven architecture.

Updated: Added kb (knowledge base) domain router to mount_cloud().
Updated: 2026-04-19 (Cluster C / PR1) — Mounted ee.cloud.kb.knowledge_router,
    which exposes GET /api/v1/knowledge/articles as a workspace-level aggregate.
Updated: 2026-04-19 (Cluster B) — Added pocket journal SSE stream router to
    mount_cloud() — feeds the RippleGraphWidget with a live, pocket-scoped
    slice of the org journal.

Domains: auth, workspace, chat, pockets, sessions, agents, kb, knowledge.
Each has router.py (thin), service.py (logic), schemas.py (validation).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ee.cloud.shared.errors import CloudError


def init_realtime() -> None:
    """Initialise the realtime EventBus. Idempotent."""
    import logging
    import os

    from ee.cloud.chat.group_service import GroupService
    from ee.cloud.chat.ws import manager as _conn_manager
    from ee.cloud.realtime.audience import AudienceResolver
    from ee.cloud.realtime.bus import InProcessBus, set_bus, set_resolver
    from ee.cloud.workspace.service import WorkspaceService

    logger = logging.getLogger(__name__)

    resolver = AudienceResolver(
        group_members=GroupService.list_member_ids,
        workspace_members=WorkspaceService.list_member_ids,
        workspace_admins=WorkspaceService.list_admin_ids,
        workspace_peers=WorkspaceService.list_peer_ids,
    )

    mode = os.environ.get("POCKETPAW_REALTIME_BUS", "inprocess").lower()
    if mode not in {"inprocess", ""}:
        logger.warning(
            "POCKETPAW_REALTIME_BUS=%s is not yet supported (RedisBus lands in Task 33);"
            " falling back to InProcessBus",
            mode,
        )

    set_bus(InProcessBus(resolver=resolver, conn_manager=_conn_manager))
    set_resolver(resolver)


def mount_cloud(app: FastAPI) -> None:
    """Mount all cloud domain routers and the error handler."""

    # Global error handler
    @app.exception_handler(CloudError)
    async def cloud_error_handler(request: Request, exc: CloudError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    # Import and mount domain routers
    from ee.cloud.agents.router import router as agents_router
    from ee.cloud.auth.router import router as auth_router
    from ee.cloud.chat.router import router as chat_router
    from ee.cloud.license import get_license_info
    from ee.cloud.pockets.router import router as pockets_router
    from ee.cloud.sessions.router import router as sessions_router
    from ee.cloud.workspace.router import router as workspace_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(workspace_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(pockets_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")

    from ee.cloud.files.router import router as files_router
    from ee.cloud.kb.knowledge_router import router as knowledge_router

    # Pocket journal SSE stream — feeds the RippleGraphWidget (Cluster B §11).
    # Lives alongside pockets_router rather than inside it so the main pocket
    # CRUD router stays focused on documents while the stream has its own
    # lifecycle (long-lived connection, polling loop, separate error paths).
    from ee.cloud.pockets.journal_stream_router import (
        router as pockets_journal_stream_router,
    )

    app.include_router(pockets_journal_stream_router, prefix="/api/v1")

    from ee.cloud.kb.router import router as kb_router
    from ee.cloud.notifications.router import router as notifications_router
    from ee.cloud.uploads.router import router as uploads_router
    from ee.paw_print.router import router as paw_print_router

    app.include_router(kb_router, prefix="/api/v1")
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(uploads_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(files_router, prefix="/api/v1")
    # paw_print lives outside ee/cloud/ but is mounted alongside the cloud
    # routers so the admin UI (paw-enterprise /pockets/<id> Paw Print tab) can
    # reach /api/v1/paw-print/* without a second app setup entry point.
    app.include_router(paw_print_router, prefix="/api/v1")

    # User search endpoint — used by group settings, pocket sharing
    from ee.cloud.models.user import User as UserModel
    from ee.cloud.shared.deps import current_user, current_workspace_id

    @app.get("/api/v1/users", tags=["Users"])
    async def search_users(
        search: str = "",
        limit: int = 10,
        user: UserModel = Depends(current_user),
        workspace_id: str = Depends(current_workspace_id),
    ):
        import re

        query = {"workspaces.workspace": workspace_id}
        if search:
            pattern = re.compile(re.escape(search), re.IGNORECASE)
            query["$or"] = [
                {"email": {"$regex": pattern}},
                {"full_name": {"$regex": pattern}},
            ]
        users = await UserModel.find(query).limit(limit).to_list()
        return [
            {
                "_id": str(u.id),
                "email": u.email,
                "name": u.full_name,
                "avatar": u.avatar,
                "status": u.status,
            }
            for u in users
        ]

    # Serve uploaded avatars from ~/.pocketpaw/uploads/
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    uploads_dir = Path.home() / ".pocketpaw" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    # Mount WebSocket at root path (not under /api/v1 prefix)
    # so frontend can connect to ws://host/ws/cloud?token=...
    from ee.cloud.chat.router import websocket_endpoint

    app.add_api_websocket_route("/ws/cloud", websocket_endpoint)

    # License endpoint (no auth)
    @app.get("/api/v1/license", tags=["License"])
    async def license_info():
        return get_license_info()

    # Register cross-domain event handlers + agent bridge
    from ee.cloud.shared.event_handlers import register_event_handlers

    register_event_handlers()

    from ee.cloud.shared.agent_bridge import register_agent_bridge

    register_agent_bridge()

    # Initialise the realtime EventBus eagerly.
    #
    # The host app (src/pocketpaw/dashboard.py) uses a FastAPI ``lifespan``
    # context manager, which supersedes ``@app.on_event("startup")`` —
    # startup handlers registered here never fire. ``init_realtime()`` only
    # sets module-level singletons (bus + resolver) with no async work, so
    # it's safe to call synchronously at mount time. Without this, the
    # first service that calls ``emit(...)`` fails with "EventBus not
    # initialized".
    init_realtime()

    # Start/stop agent pool with app lifecycle.
    #
    # Chat persistence lives entirely in ``MongoMemoryStore.save`` — it
    # writes the message row, auto-creates/touches the linked Session, and
    # receives attachments via ``InboundMessage.metadata["attachments"]``.
    # The old ``ee.cloud.shared.chat_persistence`` bus subscriber was
    # removed because it dual-wrote every turn.
    @app.on_event("startup")
    async def _start_agent_pool():
        from pocketpaw.agents.pool import get_agent_pool

        await get_agent_pool().start()

    @app.on_event("shutdown")
    async def _stop_agent_pool():
        from pocketpaw.agents.pool import get_agent_pool

        await get_agent_pool().stop()
