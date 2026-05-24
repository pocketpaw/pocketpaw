"""PocketPaw Enterprise Cloud — domain-driven architecture.

Modified: 2026-05-24 (#1202) — Registers ``register_audit_bridge`` during
    ``mount_cloud`` so every ``security.audit.AuditLogger.log()`` call from
    EE cloud writers (pocket actions, source runs, skills config, …) is
    mirrored into ``pocketpaw.audit.store.AuditStore`` — the SQLite sink
    the ``GET /api/v1/audit`` reader actually queries. Without this the
    JSONL and SQLite sinks lived in parallel and the GET surface always
    returned 0 rows even when ``~/.pocketpaw/audit.jsonl`` was full.
Updated: 2026-05-22 (feat/api-skills, Increment 2b) — mounts the Skills
    entity at ``/api/v1/skills`` (POST /skills/api-doc), the per-backend
    API-skill install endpoint that turns a pocket backend's OpenAPI
    document into a loadable SKILL.md for the authoring agent.
Updated: 2026-05-22 (RFC 05 M2b.2) — Mounts the pocket-outcomes entity at
    ``/api/v1/outcomes`` (the count surface over the per-workspace
    outcome ledger) and registers its ``pocket.outcome`` bus subscriber
    (``outcomes_service.record_outcome``) after ``init_realtime`` so a
    successful gated/direct write appends to the ledger.
Updated: 2026-05-17 — Mounts the workspace-scoped Audit entity at
    ``/api/v1/audit`` (B1) with tenancy from ``RequestContext.workspace_id``,
    the legacy ``/api/v1/runtime/audit`` remaining live; also mounts
    ``CSRFMiddleware`` and the ``/auth/csrf`` token endpoint (#1117) so
    cookie-auth callers echo ``X-CSRF-Token`` while Bearer-auth clients
    (Tauri, MCP, scripts) bypass entirely — see ``ee/cloud/_core/csrf.py``.
Updated: 2026-05-17 (pocketpaw#1118 P1) — Mounts the planner router
    (``/api/v1/planner/run``, ``/api/v1/planner/by-project/{id}``).
    The planner module wraps the OSS deep_work planner and lands its
    output into cloud Projects / Tasks / FileUploads so workspace
    operators can plan a project from Mission Control without
    crossing into the OSS local-filesystem state.
Updated: 2026-05-16 — Mission Control backend completion. Mounts the
    Projects entity (workspace > project > pocket/task/cycle hierarchy)
    and wires the in-process daily-snapshot scheduler, gated on
    ``POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`` (default false). Each
    child entity (Pocket / Task / Cycle) now carries an optional
    ``project_id`` reference; project delete soft-unassigns rather than
    cascading the underlying work.
Updated: 2026-05-13 — Mission Control cleanup PR. Lifted the 501 stubs
    on Mission Control's bulk-reassign / bulk-snooze (they now delegate
    to the Tasks service), added emit-or-no-event comments to the bulk
    approve/reject paths and the cycle counter-sync read, documented the
    snapshot_job's wiring placeholder in ``mount_cloud``, and pinned the
    UTC weekend-flag drift note onto the cycle snapshot docstring.
Updated: 2026-05-13 — Mission Control PR 2 of 3. Added the Tasks
    entity (unified work-item primitive: Nudges + agent tasks +
    projections) and its in-process listener that fans ``task.proposed``
    out to human assignees via the existing notifications surface.
Updated: 2026-05-13 — Mission Control PR 3. Mounts the Cycles router on
    ``mount_cloud()``. The Cycles daily-snapshot job lives at
    ``ee.cloud.cycles.snapshot_job`` and is invoked by the host platform's
    scheduler (cron / Kubernetes CronJob / Celery beat) rather than wired
    as an in-process loop — see that module's docstring for rationale.
Updated: 2026-04-30 — Stage 1.B of "Files as Knowledge". Wires
    ``register_upload_listeners`` into ``mount_cloud`` so the FileReady
    bus subscriber drives KB indexing for every workspace upload.
Updated: Added kb (knowledge base) domain router to mount_cloud().
Updated: 2026-04-19 (Cluster C / PR1) — Mounted ee.cloud.kb.knowledge_router,
    which exposes GET /api/v1/knowledge/articles as a workspace-level aggregate.
Updated: 2026-04-19 (Cluster B) — Added pocket journal SSE stream router to
    mount_cloud() — feeds the RippleGraphWidget with a live, pocket-scoped
    slice of the org journal.
Updated: 2026-05-13 (feat/mission-control-facade) — mounted the Mission
    Control façade router at /api/v1/mission-control/* and wired the
    in-process activity buffer's bus subscribers after init_realtime so
    the live ticker fills in from agent.thinking / agent.tool_use /
    agent.stream_end events. PR 1 of three; Tasks (PR 2) and Cycles
    (PR 3) plug into the same façade in follow-ups.

Domains: auth, workspace, chat, pockets, sessions, agents, kb, knowledge,
mission_control, cycles, tasks.
Each has router.py (thin), service.py (logic), schemas.py (validation).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI


def init_realtime() -> None:
    """Initialise the realtime EventBus. Idempotent."""
    import logging
    import os

    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.chat.ws import manager as _conn_manager
    from pocketpaw_ee.cloud.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud.realtime.bus import InProcessBus, set_bus, set_resolver
    from pocketpaw_ee.cloud.workspace import service as workspace_service

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
    from pocketpaw_ee.cloud._core.csrf import CSRFMiddleware, csrf_router
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud._core.timing import TimingMiddleware

    # Starlette's add_middleware is a stack — LAST registered runs OUTERMOST
    # on inbound. Effective order here: CSRF → Timing → route handler.
    # A CSRF 403 short-circuits before Timing observes the request, so perf
    # data won't include rejected POSTs. That's a deliberate tradeoff: the
    # CSRF gate exists to be fast and predictable, not measured. Reorder
    # ONLY if you want Timing to wrap CSRF rejections (swap the two add_
    # middleware calls — TimingMiddleware would then run outermost).
    app.add_middleware(TimingMiddleware)

    # CSRF middleware — outermost on inbound, runs before any route.
    # Cookie-auth callers must echo X-CSRF-Token; Bearer-auth callers
    # (Tauri, MCP, scripts) bypass entirely. See ``ee/cloud/_core/csrf.py``.
    app.add_middleware(CSRFMiddleware)

    # Global error handler — extracted to ee.cloud._core.http
    add_error_handler(app)

    # Eager-import ripple_sources so @register decorators run at startup
    # rather than on first pocket get(). Keeps ``_REGISTRY`` populated
    # for any startup self-checks that inspect it.
    import pocketpaw_ee.cloud.ripple_sources  # noqa: F401

    # Import and mount domain routers
    from pocketpaw_ee.cloud.agents.router import router as agents_router
    from pocketpaw_ee.cloud.audit.router import router as audit_router
    from pocketpaw_ee.cloud.auth.router import router as auth_router
    from pocketpaw_ee.cloud.chat.router import router as chat_router
    from pocketpaw_ee.cloud.connectors.router import router as connectors_router
    from pocketpaw_ee.cloud.cycles.router import router as cycles_router
    from pocketpaw_ee.cloud.license import get_license_info
    from pocketpaw_ee.cloud.planner.router import router as planner_router
    from pocketpaw_ee.cloud.pockets.chat_router import router as pocket_chat_router
    from pocketpaw_ee.cloud.pockets.router import router as pockets_router
    from pocketpaw_ee.cloud.projects.router import router as projects_router
    from pocketpaw_ee.cloud.sessions.router import router as sessions_router
    from pocketpaw_ee.cloud.skills.router import router as skills_router
    from pocketpaw_ee.cloud.workspace.router import router as workspace_router

    app.include_router(auth_router, prefix="/api/v1")
    # CSRF token-mint endpoint sits alongside the rest of /auth/*.
    app.include_router(csrf_router, prefix="/api/v1")
    app.include_router(workspace_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(connectors_router, prefix="/api/v1")
    app.include_router(pockets_router, prefix="/api/v1")
    # Pocket chat — agent-driven pocket creation SSE stream (POST /pockets/chat).
    app.include_router(pocket_chat_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(planner_router, prefix="/api/v1")
    app.include_router(sessions_router, prefix="/api/v1")
    app.include_router(cycles_router, prefix="/api/v1")
    # Skills — per-backend API-skill install (POST /skills/api-doc).
    app.include_router(skills_router, prefix="/api/v1")

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

    from pocketpaw_ee.cloud.files.router import router as files_router
    from pocketpaw_ee.cloud.kb.knowledge_router import router as knowledge_router

    # Pocket journal SSE stream — feeds the RippleGraphWidget (Cluster B §11).
    # Lives alongside pockets_router rather than inside it so the main pocket
    # CRUD router stays focused on documents while the stream has its own
    # lifecycle (long-lived connection, polling loop, separate error paths).
    from pocketpaw_ee.cloud.pockets.journal_stream_router import (
        router as pockets_journal_stream_router,
    )

    app.include_router(pockets_journal_stream_router, prefix="/api/v1")

    from pocketpaw_ee.cloud.kb.router import router as kb_router
    from pocketpaw_ee.cloud.livekit.router import router as livekit_router
    from pocketpaw_ee.cloud.mission_control.router import router as mission_control_router
    from pocketpaw_ee.cloud.notifications.router import router as notifications_router
    from pocketpaw_ee.cloud.outcomes.router import router as outcomes_router
    from pocketpaw_ee.cloud.tasks.router import router as tasks_router
    from pocketpaw_ee.cloud.uploads.router import router as uploads_router
    from pocketpaw_ee.fabric.router import router as fabric_router
    from pocketpaw_ee.fleet.router import router as fleet_router
    from pocketpaw_ee.instinct.router import router as instinct_router
    from pocketpaw_ee.paw_print.router import router as paw_print_router

    app.include_router(kb_router, prefix="/api/v1")
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(uploads_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(files_router, prefix="/api/v1")
    app.include_router(mission_control_router, prefix="/api/v1")
    app.include_router(livekit_router, prefix="/api/v1")
    # Pocket outcomes — GET /api/v1/outcomes count surface (RFC 05 M2b.2).
    app.include_router(outcomes_router, prefix="/api/v1")

    # Files Tab v2 — /api/v1/files/tree + /api/v1/files/browse. Mounted
    # inline (instead of via build_router's ctx_factory) so the routes can
    # use the canonical `Depends(current_active_user)` auth chain without
    # resolving fastapi-users dependencies manually from the Request.
    from typing import Any

    from fastapi import APIRouter as _APIRouter
    from fastapi import Depends as _Depends
    from fastapi import HTTPException as _HTTPException
    from fastapi import Query as _Query

    from pocketpaw_ee.cloud.auth.core import current_active_user as _current_active_user
    from pocketpaw_ee.cloud.files.abac_config import load_rules as _load_abac_rules
    from pocketpaw_ee.cloud.files.browse import browse_mount as _browse_mount
    from pocketpaw_ee.cloud.files.dto import RequestContext as _RequestContext
    from pocketpaw_ee.cloud.files.errors import FilesError as _FilesError
    from pocketpaw_ee.cloud.files.errors import MountNotFound as _MountNotFound
    from pocketpaw_ee.cloud.files.mounts_config import load_mounts as _load_mounts
    from pocketpaw_ee.cloud.files.providers.kb import KbProvider as _KbProvider
    from pocketpaw_ee.cloud.files.providers.uploads import UploadsProvider as _UploadsProvider
    from pocketpaw_ee.cloud.files.registry import ProviderRegistry as _ProviderRegistry
    from pocketpaw_ee.cloud.files.tree import CachedTreeBuilder as _CachedTreeBuilder
    from pocketpaw_ee.cloud.models.user import User as _User
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore as _UploadsStore

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

    # Fabric / Fleet / Instinct also live outside ee/cloud/ (pocketpaw_ee.
    # {fabric,fleet,instinct}). Their logic split into the OSS core in Phase 2,
    # but the HTTP routers stay enterprise — they depend on cloud auth — so the
    # OSS core no longer mounts them. They ride along here, like paw_print
    # above, instead of through the core's mount_v1_routers().
    app.include_router(fabric_router, prefix="/api/v1")
    app.include_router(fleet_router, prefix="/api/v1")
    app.include_router(instinct_router, prefix="/api/v1")

    # Calendar router declares its own full prefix (/api/v1/calendar) so it
    # is mounted without an additional prefix here. See ee/calendar/router.py.
    from pocketpaw_ee.calendar import router as calendar_router

    app.include_router(calendar_router)

    # User search endpoint — used by group settings, pocket sharing
    from pocketpaw_ee.cloud.models.user import User as UserModel
    from pocketpaw_ee.cloud.shared.deps import (
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
        from pocketpaw_ee.cloud._core.timing import percentiles, snapshot

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
    from pocketpaw_ee.cloud.chat.router import websocket_endpoint

    app.add_api_websocket_route("/ws/cloud", websocket_endpoint)

    # License endpoint (no auth)
    @app.get("/api/v1/license", tags=["License"])
    async def license_info():
        return get_license_info()

    # Register cross-domain event handlers + agent bridge
    from pocketpaw_ee.cloud.shared.event_handlers import register_event_handlers

    register_event_handlers()

    from pocketpaw_ee.cloud.shared.agent_bridge import register_agent_bridge

    register_agent_bridge()

    # NOTE: Composio is wired per-backend via ``pocketpaw_ee.cloud.composio.providers``
    # — each agent backend (claude_sdk, openai_agents, google_adk,
    # deep_agents) calls ``build_tools_for_backend()`` in its own tool-build
    # path to fetch Composio tools using the official provider package for
    # that SDK. No cloud-bootstrap registration is needed.

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

    # Bridge ``AuditLogger`` (JSONL) writes into ``AuditStore`` (SQLite).
    # Cloud writers across the EE codebase (pockets/action_executor,
    # pockets/source_executor, pockets/service, skills/service,
    # agent/pocket_router) all call ``get_audit_logger().log(...)``, but the
    # ``GET /api/v1/audit`` reader (``ee.cloud.audit.service``) reads from
    # ``get_audit_store()`` — a totally separate sink. Without this bridge
    # the reader returned 0 rows for every query (#1202). Idempotent via a
    # module-level flag so re-mounting (tests) does not double-mirror.
    from pocketpaw_ee.cloud.audit.listeners import register_audit_bridge

    register_audit_bridge()

    # Register in-process bus subscribers (Stage 1.B "Files as Knowledge").
    # The FileReady listener drives KB indexing for every workspace upload.
    # Must run after ``init_realtime`` because subscriptions go on the
    # singleton bus that init_realtime installs.
    from pocketpaw_ee.cloud.uploads.listeners import register_upload_listeners

    register_upload_listeners()

    # Pocket outcomes ledger subscriber (RFC 05 M2b.2). Appends every
    # ``pocket.outcome`` event to its workspace-scoped JSONL ledger so
    # ``GET /api/v1/outcomes`` can count business outcomes. Same
    # constraint as the upload listeners — subscribe AFTER init_realtime
    # installed the singleton bus.
    from pocketpaw_ee.cloud._core.realtime.bus import get_bus as _get_bus
    from pocketpaw_ee.cloud.outcomes import service as _outcomes_service

    _get_bus().subscribe("pocket.outcome", _outcomes_service.record_outcome)

    # Tasks → notifications fan-out. When a Task is proposed to a human
    # assignee, drop an in-app notification so they see it even without
    # Mission Control open. Agent assignees skip this path — they pick
    # up work via the claim flow.
    from pocketpaw_ee.cloud.tasks.listeners import register_task_listeners

    register_task_listeners()

    # In-process daily-snapshot scheduler — opt-in via env var.
    #
    # Default OFF in tests + dev (each pytest run would otherwise spawn a
    # background loop that outlives the test). Production deployments set
    # ``POCKETPAW_CLOUD_SCHEDULER_ENABLED=true`` to flip it on. Hosts that
    # prefer external cron / Kubernetes CronJob / Celery beat (see the
    # ``ee.cloud.cycles.snapshot_job`` docstring) leave the flag unset
    # and dispatch the same ``snapshot_all_active`` callable from their
    # platform scheduler.
    import os as _os

    if _os.environ.get("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "").lower() == "true":
        from pocketpaw_ee.cloud.cycles.scheduler import start_in_process_scheduler

        @app.on_event("startup")
        async def _start_cycle_scheduler() -> None:
            await start_in_process_scheduler(app)

        @app.on_event("shutdown")
        async def _stop_cycle_scheduler() -> None:
            from pocketpaw_ee.cloud.cycles.scheduler import stop_in_process_scheduler

            await stop_in_process_scheduler(app)

    # Mission Control activity buffer — per-workspace ring buffer fed by
    # agent.* bus events. Same constraint as the upload listeners: subscribe
    # AFTER init_realtime installed the singleton bus.
    from pocketpaw_ee.cloud.activity.buffer import register_activity_listeners

    register_activity_listeners()

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
