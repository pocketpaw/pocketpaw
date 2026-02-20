"""Lightweight API-only server for ``pocketpaw serve``.

Starts only the versioned ``/api/v1/`` routers with auth middleware and CORS —
no web dashboard, no WebSocket handler, no frontend assets.  Ideal for headless
deployments, CI runners, or when an external client (Tauri, scripts) only needs
the REST API.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def create_api_app():
    """Build a minimal FastAPI application with only v1 API routers."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from pocketpaw.api.v1 import mount_v1_routers
    from pocketpaw.config import Settings

    app = FastAPI(
        title="PocketPaw API",
        description="Self-hosted AI agent — REST-only server (no dashboard).",
        version="1.0.0",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )

    # --- CORS -----------------------------------------------------------
    _BUILTIN_ORIGINS = [
        "tauri://localhost",
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
        allow_origin_regex=(
            r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
            r"|^https://[a-zA-Z0-9-]+\.trycloudflare\.com$"
        ),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # --- Auth middleware -------------------------------------------------
    from pocketpaw.dashboard import auth_middleware

    app.middleware("http")(auth_middleware)

    # --- Mount all /api/v1/ routers -------------------------------------
    mount_v1_routers(app)

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
