"""PocketPaw Enterprise Cloud — domain-driven architecture.

Updated: 2026-04-30 — Stage 1.B of "Files as Knowledge". Wires
    ``register_upload_listeners`` into ``mount_cloud`` so the FileReady
    bus subscriber drives KB indexing for every workspace upload.
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

from fastapi import Depends, FastAPI


def init_realtime() -> None:
    """Initialise the realtime EventBus. Idempotent."""
    import logging
    import os

    from ee.cloud.chat import group_service
    from ee.cloud.chat.ws import manager as _conn_manager
    from ee.cloud.realtime.audience import AudienceResolver
    from ee.cloud.realtime.bus import InProcessBus, set_bus, set_resolver
    from ee.cloud.workspace import service as workspace_service

    logger = logging.getLogger(__name__)

    resolver = AudienceResolver(
        group_members=group_service.list_member_ids,
        workspace_members=workspace_service.list_member_ids,
        workspace_admins=workspace_service.list_admin_ids,
        workspace_peers=workspace_service.list_peer_ids,
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
    """Mount all cloud domain routers, the error handler, and the
    request-timing middleware."""
    from ee.cloud._core.http import add_error_handler
    from ee.cloud._core.timing import TimingMiddleware

    # Request-timing middleware first so it wraps every subsequent route
    app.add_middleware(TimingMiddleware)

    # Global error handler — extracted to ee.cloud._core.http
    add_error_handler(app)

    # Eager-import ripple_sources so @register decorators run at startup
    # rather than on first pocket get(). Keeps ``_REGISTRY`` populated
    # for any startup self-checks that inspect it.
    import ee.cloud.ripple_sources  # noqa: F401

    # Import and mount domain routers
    from ee.cloud.agents.router import router as agents_router
    from ee.cloud.auth.router import router as auth_router
    from ee.cloud.chat.router import router as chat_router
    from ee.cloud.connectors.router import router as connectors_router
    from ee.cloud.license import get_license_info
    from ee.cloud.pockets.router import router as pockets_router
    from ee.cloud.sessions.router import router as sessions_router
    from ee.cloud.workspace.router import router as workspace_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(workspace_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(pockets_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")

    # Phase 1 PR-8: register the connector bus listener so local-mode
    # CLI actions (firebase, gcp, …) get picked up by the in-process
    # runtime. In multi-tenant deployments this becomes a cross-process
    # listener once Task 33 ships RedisBus; the contract is identical.
    try:
        from pocketpaw.runtime.connector_bus import register_listener

        register_listener()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "connector bus listener failed to register",
            exc_info=True,
        )

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

    # Files Tab v2 — /api/v1/files/tree + /api/v1/files/browse. Mounted
    # inline (instead of via build_router's ctx_factory) so the routes can
    # use the canonical `Depends(current_active_user)` auth chain without
    # resolving fastapi-users dependencies manually from the Request.
    from typing import Any

    from fastapi import APIRouter as _APIRouter
    from fastapi import Depends as _Depends
    from fastapi import HTTPException as _HTTPException
    from fastapi import Query as _Query

    from ee.cloud.auth.core import current_active_user as _current_active_user
    from ee.cloud.files.abac_config import load_rules as _load_abac_rules
    from ee.cloud.files.browse import browse_mount as _browse_mount
    from ee.cloud.files.dto import RequestContext as _RequestContext
    from ee.cloud.files.errors import FilesError as _FilesError
    from ee.cloud.files.errors import MountNotFound as _MountNotFound
    from ee.cloud.files.mounts_config import load_mounts as _load_mounts
    from ee.cloud.files.providers.kb import KbProvider as _KbProvider
    from ee.cloud.files.providers.uploads import UploadsProvider as _UploadsProvider
    from ee.cloud.files.registry import ProviderRegistry as _ProviderRegistry
    from ee.cloud.files.tree import CachedTreeBuilder as _CachedTreeBuilder
    from ee.cloud.models.user import User as _User
    from ee.cloud.uploads.mongo_store import MongoFileStore as _UploadsStore

    class _NoopKbService:
        async def list_documents(self, workspace_id: str, *, limit: int = 500):
            return []

        async def get_document(self, doc_id: str, *, workspace_id: str):
            raise KeyError(doc_id)

    _files_registry = _ProviderRegistry(configs=_load_mounts())
    _files_registry.register(_UploadsProvider(store=_UploadsStore()))
    _files_registry.register(_KbProvider(service=_NoopKbService()))
    _files_rules = _load_abac_rules()
    _files_tree_builder = _CachedTreeBuilder(registry=_files_registry, rules=_files_rules)

    def _files_ctx_from_user(user: _User) -> _RequestContext:
        role = ""
        for ws in getattr(user, "workspaces", []) or []:
            ws_id = getattr(ws, "workspace", None) or getattr(ws, "workspace_id", None)
            if ws_id == user.active_workspace:
                role = getattr(ws, "role", "") or ""
                break
        return _RequestContext(
            user_id=str(user.id),
            workspace_id=user.active_workspace,
            attributes={"role": role},
        )

    _files_v2 = _APIRouter(prefix="/files", tags=["Files"])

    @_files_v2.get("/tree")
    async def _files_get_tree(
        workspace_id: str | None = _Query(None),
        user: _User = _Depends(_current_active_user),
    ) -> dict[str, Any]:
        ctx = _files_ctx_from_user(user)
        if workspace_id is not None and workspace_id != ctx.workspace_id:
            raise _HTTPException(status_code=403, detail="files.workspace_mismatch")
        tree, warnings = await _files_tree_builder.build(ctx=ctx, collect_warnings=True)
        return {**tree.model_dump(), "warnings": warnings}

    @_files_v2.get("/browse")
    async def _files_get_browse(
        mount: str = _Query(...),
        cursor: str | None = _Query(None),
        limit: int = _Query(50, ge=1, le=500),
        workspace_id: str | None = _Query(None),
        user: _User = _Depends(_current_active_user),
    ) -> dict[str, Any]:
        ctx = _files_ctx_from_user(user)
        if workspace_id is not None and workspace_id != ctx.workspace_id:
            raise _HTTPException(status_code=403, detail="files.workspace_mismatch")
        variables = {"workspace_id": ctx.workspace_id or ""}
        try:
            page = await _browse_mount(
                ctx=ctx,
                registry=_files_registry,
                rules=_files_rules,
                mount_path=mount,
                variables=variables,
                cursor=cursor,
                limit=limit,
                filters={},
            )
        except _MountNotFound:
            raise _HTTPException(status_code=404, detail="files.mount_not_found") from None
        except _FilesError as e:
            raise _HTTPException(status_code=e.http_status, detail=e.code) from e
        return page.model_dump()

    app.include_router(_files_v2, prefix="/api/v1")
    # paw_print lives outside ee/cloud/ but is mounted alongside the cloud
    # routers so the admin UI (paw-enterprise /pockets/<id> Paw Print tab) can
    # reach /api/v1/paw-print/* without a second app setup entry point.
    app.include_router(paw_print_router, prefix="/api/v1")

    # User search endpoint — used by group settings, pocket sharing
    from ee.cloud.models.user import User as UserModel
    from ee.cloud.shared.deps import (
        current_user,
        current_workspace_id,
        require_action_any_workspace,
    )

    # Admin perf endpoint — dumps the in-memory request-timing buffer
    # populated by ``_core.timing.TimingMiddleware`` (Phase 0). The Phase 11
    # perf pass uses this to identify hot endpoints from production load
    # before optimizing. Gated on ``admin.perf`` (owner-only) — per-route
    # timing reveals traffic patterns and shouldn't be visible to every
    # admin in a workspace.
    @app.get("/api/v1/_admin/perf", tags=["Admin"])
    async def perf_report(
        _user: UserModel = Depends(require_action_any_workspace("admin.perf")),
    ) -> dict[str, Any]:
        from ee.cloud._core.timing import percentiles, snapshot

        snap = snapshot()
        return {
            "endpoints": [
                {
                    "method": method,
                    "path": path,
                    "count": len(samples),
                    "p50_ms": round(percentiles(samples)[0.5], 2),
                    "p95_ms": round(percentiles(samples)[0.95], 2),
                    "p99_ms": round(percentiles(samples)[0.99], 2),
                }
                for (method, path), samples in sorted(
                    snap.items(),
                    key=lambda kv: percentiles(kv[1])[0.95],
                    reverse=True,
                )
            ],
            "total_endpoints": len(snap),
        }

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

    # Register in-process bus subscribers (Stage 1.B "Files as Knowledge").
    # The FileReady listener drives KB indexing for every workspace upload.
    # Must run after ``init_realtime`` because subscriptions go on the
    # singleton bus that init_realtime installs.
    from ee.cloud.uploads.listeners import register_upload_listeners

    register_upload_listeners()

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
