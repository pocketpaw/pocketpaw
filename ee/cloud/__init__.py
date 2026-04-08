"""PocketPaw Enterprise Cloud — domain-driven architecture.

Updated: Added kb (knowledge base) domain router to mount_cloud().

Domains: auth, workspace, chat, pockets, sessions, agents, kb.
Each has router.py (thin), service.py (logic), schemas.py (validation).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ee.cloud.shared.errors import CloudError


def mount_cloud(app: FastAPI) -> None:
    """Mount all cloud domain routers and the error handler."""

    # Global error handler
    @app.exception_handler(CloudError)
    async def cloud_error_handler(request: Request, exc: CloudError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    # Import and mount domain routers
    from ee.cloud.auth.router import router as auth_router
    from ee.cloud.workspace.router import router as workspace_router
    from ee.cloud.agents.router import router as agents_router
    from ee.cloud.chat.router import router as chat_router
    from ee.cloud.pockets.router import router as pockets_router
    from ee.cloud.sessions.router import router as sessions_router
    from ee.cloud.license import get_license_info

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(workspace_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(pockets_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")

    from ee.cloud.kb.router import router as kb_router
    app.include_router(kb_router, prefix="/api/v1")

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

    # Start/stop agent pool with app lifecycle + chat persistence
    @app.on_event("startup")
    async def _start_agent_pool():
        # Register chat persistence bridge (saves runtime WS messages to MongoDB)
        from ee.cloud.shared.chat_persistence import register_chat_persistence
        register_chat_persistence()
        from pocketpaw.agents.pool import get_agent_pool
        await get_agent_pool().start()

    @app.on_event("shutdown")
    async def _stop_agent_pool():
        from pocketpaw.agents.pool import get_agent_pool
        await get_agent_pool().stop()
