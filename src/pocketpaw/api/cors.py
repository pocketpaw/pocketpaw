"""Shared CORS policy for the two app factories (``api/serve.py``, ``dashboard.py``).

Created: 2026-08-24 (fix/cors-headers-on-unhandled-500).

Why this module exists — the bug it closes:

``CORSMiddleware`` only attaches ``Access-Control-Allow-Origin`` to responses that
travel back out *through* it. Starlette builds its stack as

    ServerErrorMiddleware
      -> [user middleware, incl. CORSMiddleware]
        -> ExceptionMiddleware -> router

so an exception with no registered handler propagates PAST ``CORSMiddleware``
(nothing was sent through it) and ``ServerErrorMiddleware`` — which sits OUTSIDE
it — mints the 500 itself. That 500 carries no CORS headers, and a browser
reports the result as::

    Access to fetch at '<url>' from origin '<origin>' has been blocked by CORS
    policy: No 'Access-Control-Allow-Origin' header is present on the requested
    resource.

which is a lie: the CORS config is fine, the request 500'd. Every server-side
crash on a cross-origin call was being reported to the user as a CORS
misconfiguration, hiding the real failure. Reproduced against the deployed
backend on ``POST /api/v1/sites/by-pocket/{id}/editable``.

The fix: register an ``Exception`` handler. FastAPI hands that handler to
``ServerErrorMiddleware``, so we mint the 500 ourselves and attach the CORS
headers by hand (the middleware cannot do it for us at that point). Starlette
still re-raises the original exception afterwards, so logging, ``TestClient``'s
``raise_server_exceptions``, and every existing traceback path are unchanged.

The response body carries BOTH shapes the frontend understands: ``error`` (the
machine-readable cloud envelope) and ``detail`` (the key ``friendlyErrorMessage``
in paw-enterprise actually reads).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_ORIGINS",
    "ORIGIN_REGEX",
    "allowed_origins",
    "install_cors",
    "origin_allowed",
]

# Desktop/dev origins that are always allowed; deployments add their own via
# ``POCKETPAW_API_CORS_ALLOWED_ORIGINS`` (Settings.api_cors_allowed_origins).
BUILTIN_ORIGINS = [
    "tauri://localhost",
    "https://tauri.localhost",  # Tauri v2
    "http://localhost:1420",  # Tauri dev server
]

# Any localhost / 127.0.0.1 origin on any port or scheme.
ORIGIN_REGEX = r"^https?://([a-z]+\.)?localhost(:\d+)?$|^https?://127\.0\.0\.1(:\d+)?$"

_ORIGIN_RE = re.compile(ORIGIN_REGEX)


def allowed_origins() -> list[str]:
    """Builtin origins plus the deployment's configured extras.

    Settings load failures are non-fatal — a misconfigured settings file must
    not take the whole app down at import time, it just means no extra origins.
    """
    from pocketpaw.config import Settings

    try:
        custom = Settings.load().api_cors_allowed_origins
    except Exception as exc:  # noqa: BLE001 - never block app construction
        logger.debug("Failed to load custom CORS origins: %s", exc)
        custom = []
    return list(set(BUILTIN_ORIGINS + list(custom or [])))


def origin_allowed(origin: str, origins: list[str]) -> bool:
    """Mirror Starlette's own check: exact match, wildcard, or the regex.

    Starlette uses ``fullmatch`` for ``allow_origin_regex``; match that exactly
    so the 500 path can never be more permissive than the middleware.
    """
    if not origin:
        return False
    return "*" in origins or origin in origins or _ORIGIN_RE.fullmatch(origin) is not None


def install_cors(app: FastAPI) -> None:
    """Add ``CORSMiddleware`` and the CORS-aware unhandled-error handler.

    Call this LAST among ``add_middleware`` calls so CORS is the outermost user
    middleware — every inner rejection (auth 401, CSRF 403, a mapped
    ``CloudError``) then ships its ``Access-Control-Allow-Origin`` on the way
    out. The error handler covers the one case the middleware structurally
    cannot: the unhandled 500 minted above it.
    """
    from fastapi.middleware.cors import CORSMiddleware

    origins = allowed_origins()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    add_cors_aware_error_handler(app, origins)


def add_cors_aware_error_handler(app: FastAPI, origins: list[str]) -> None:
    """Register the ``Exception`` handler that keeps CORS headers on a 500."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled error on %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal server error — see server logs for details.",
                },
                # friendlyErrorMessage (paw-enterprise) reads `detail`, not `error`.
                "detail": "Internal server error — see server logs for details.",
            },
        )
        origin = request.headers.get("origin") or ""
        if origin_allowed(origin, origins):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    app.add_exception_handler(Exception, unhandled_error_handler)  # type: ignore[arg-type]
