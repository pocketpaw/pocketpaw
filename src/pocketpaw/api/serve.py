"""API server for ``pocketpaw serve``.

Starts the versioned ``/api/v1/`` routers **and** a ``/ws`` WebSocket endpoint
with auth middleware and CORS — no web dashboard frontend assets.  Ideal for
external clients (Tauri desktop app, scripts) that provide their own UI.

The server initialises the full backend infrastructure (message bus, agent loop,
scheduler, etc.) so that both REST chat and WebSocket chat work identically to
the dashboard mode.
"""

import logging

logger = logging.getLogger(__name__)


def create_api_app():
    """Build a FastAPI application with v1 API routers and WebSocket."""
    from fastapi import FastAPI, Query, WebSocket
    from fastapi.middleware.cors import CORSMiddleware

    from pocketpaw.api.v1 import mount_v1_routers
    from pocketpaw.config import Settings, get_access_token

    app = FastAPI(
        title="PocketPaw API",
        description="Self-hosted AI agent — REST + WebSocket server for external clients.",
        version="1.0.0",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )

    # --- Middleware (order matters: last added = outermost = runs first) --
    # Auth must be added BEFORE CORS so that CORS is outermost and handles
    # OPTIONS preflight requests before auth can reject them.
    from pocketpaw.dashboard_auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)

    _BUILTIN_ORIGINS = [
        "tauri://localhost",
        "https://tauri.localhost",
        "http://localhost:1420",
    ]
    try:
        _custom = Settings.load().api_cors_allowed_origins
    except Exception:
        _custom = []
    _ORIGINS = list(set(_BUILTIN_ORIGINS + _custom))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ORIGINS,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Mount all /api/v1/ routers -------------------------------------
    mount_v1_routers(app)

    # --- WebSocket handler helper ----------------------------------------
    async def _handle_ws(
        websocket: WebSocket,
        token: str | None,
        resume_session: str | None,
    ):
        """Shared WebSocket handler for /ws and /api/v1/ws."""
        from pocketpaw.dashboard_auth import _is_genuine_localhost
        from pocketpaw.dashboard_ws import websocket_handler

        await websocket_handler(
            websocket,
            token,
            resume_session,
            _is_genuine_localhost_fn=_is_genuine_localhost,
            _get_access_token_fn=get_access_token,
        )

    # --- WebSocket endpoints (both /ws and /api/v1/ws) -------------------
    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: str | None = Query(None),
        resume_session: str | None = Query(None),
    ):
        """WebSocket endpoint — delegates to dashboard_ws.websocket_handler()."""
        await _handle_ws(websocket, token, resume_session)

    @app.websocket("/api/v1/ws")
    async def websocket_v1_endpoint(
        websocket: WebSocket,
        token: str | None = Query(None),
        resume_session: str | None = Query(None),
    ):
        """WebSocket v1 endpoint — same handler, v1-prefixed path."""
        await _handle_ws(websocket, token, resume_session)

    @app.websocket("/v1/ws")
    async def websocket_v1_short_endpoint(
        websocket: WebSocket,
        token: str | None = Query(None),
        resume_session: str | None = Query(None),
    ):
        """WebSocket v1 short path — for clients using /v1/ws."""
        await _handle_ws(websocket, token, resume_session)

    # --- Lifecycle events -----------------------------------------------
    @app.on_event("startup")
    async def startup():
        from pocketpaw.dashboard_lifecycle import startup_event

        await startup_event()

    @app.on_event("shutdown")
    async def shutdown():
        from pocketpaw.dashboard_lifecycle import shutdown_event

        await shutdown_event()

    return app


def run_api_server(
    host: str = "127.0.0.1",
    port: int = 8888,
    dev: bool = False,
) -> None:
    """Start the API-only server (no dashboard)."""
    import uvicorn

    print("\n" + "=" * 50)
    print("\U0001f43e POCKETPAW API SERVER")
    print("=" * 50)

    if host == "0.0.0.0":
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "<your-server-ip>"
        print(f"\n\U0001f310 API docs: http://{local_ip}:{port}/api/v1/docs")
        print(f"   (listening on all interfaces \u2014 {host}:{port})\n")
    else:
        print(f"\n\U0001f310 API docs: http://localhost:{port}/api/v1/docs\n")

    if dev:
        import pathlib

        src_dir = str(pathlib.Path(__file__).resolve().parent.parent)
        uvicorn.run(
            "pocketpaw.api.serve:create_api_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=[src_dir],
            reload_includes=["*.py"],
            log_level="debug",
        )
    else:
        app = create_api_app()
        uvicorn.run(app, host=host, port=port)
